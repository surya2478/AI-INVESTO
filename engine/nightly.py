"""The nightly job.

Runs unattended after market close and does two different kinds of work:

  * KEEPING CURRENT -- prices, index membership, surveillance, flows, and the
    most recent filing window. Cheap, runs in full every night.
  * BACKFILLING -- walking BSE's result announcements backwards through history
    a couple of windows at a time. BSE stops responding under sustained
    pagination (the first attempt at a 14-month backfill died on a curl timeout
    after 13 minutes), so history accumulates over successive nights instead of
    one long run. Progress is stored in `job_state`, so a missed or failed night
    costs a night, not the whole backfill.

Every stage is isolated: one failing stage is recorded and the rest still run.
A nightly job that aborts halfway is worse than one that reports partial success,
because the failure is invisible until you look for data that never arrived.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import traceback
from dataclasses import dataclass, field

from engine.config import settings
from engine.storage import db

log = logging.getLogger(__name__)

# How far back the filings backfill needs to reach.
#
# Only as far as the XBRL era, NOT the start of history. NSE's corporate-filings
# API already carries a true filingDate for every quarter up to Dec-2024, so
# those periods need nothing from BSE. BSE announcements exist here purely to
# timestamp PDF-extracted quarters, which begin in 2025. The overlap back to
# Oct-2024 is deliberate: it spans quarters where BOTH sources exist, so the two
# filing dates can be cross-checked before the PDF era is trusted on its own.
#
# Backfilling to 2016 would spend three months of nights collecting timestamps
# for data that already has them.
BACKFILL_FLOOR = dt.date(2024, 10, 1)
WINDOW_DAYS = 18
BACKFILL_JOB = "filings_backfill"


@dataclass
class StageResult:
    name: str
    status: str                    # OK | FAILED | SKIPPED
    detail: str = ""
    seconds: float = 0.0


@dataclass
class NightlyReport:
    started: dt.datetime
    stages: list[StageResult] = field(default_factory=list)

    @property
    def failed(self) -> list[StageResult]:
        return [s for s in self.stages if s.status == "FAILED"]

    def render(self) -> str:
        lines = [f"AI-Investo nightly run — {self.started:%Y-%m-%d %H:%M:%S}", "=" * 62]
        for stage in self.stages:
            mark = {"OK": "ok  ", "FAILED": "FAIL", "SKIPPED": "skip"}[stage.status]
            lines.append(f"[{mark}] {stage.name:<22} {stage.seconds:6.1f}s  {stage.detail}")
        lines.append("=" * 62)
        lines.append(
            f"{len(self.stages) - len(self.failed)}/{len(self.stages)} stages ok"
            + (f" — FAILED: {', '.join(s.name for s in self.failed)}" if self.failed else "")
        )
        return "\n".join(lines)


def _stage(report: NightlyReport, name: str, fn) -> None:
    """Run one stage, recording success or failure without propagating."""
    started = time.monotonic()
    try:
        detail = fn() or ""
        report.stages.append(StageResult(name, "OK", str(detail), time.monotonic() - started))
    except Exception as exc:  # noqa: BLE001 - a bad stage must not end the night
        log.exception("stage %s failed", name)
        report.stages.append(StageResult(
            name, "FAILED", f"{type(exc).__name__}: {exc}"[:140],
            time.monotonic() - started,
        ))


# --------------------------------------------------------------- job cursors
def get_cursor(con, job: str) -> dt.date | None:
    row = con.execute("SELECT cursor_date FROM job_state WHERE job = ?", [job]).fetchone()
    return row[0] if row and row[0] else None


def set_cursor(con, job: str, cursor: dt.date, detail: str = "") -> None:
    con.execute("DELETE FROM job_state WHERE job = ?", [job])
    con.execute("""
        INSERT INTO job_state (job, cursor_date, detail, updated_at)
        VALUES (?, ?, ?, current_timestamp)
    """, [job, cursor, detail])


# -------------------------------------------------------------------- stages
def stage_universe(con) -> str:
    from engine.providers.nse_provider import NSEProvider
    from engine.universe import builder

    provider = NSEProvider()
    symbols = builder.sync_symbol_master(con, provider)
    counts = builder.sync_index_membership(con, provider)
    levels = builder.sync_index_levels(con, provider)
    flagged = builder.sync_surveillance(con, provider)
    flows = builder.sync_flows(con, provider)
    return (f"{symbols} listings, {len(counts)} indices, {levels} index levels, "
            f"{flagged} ASM flags, {flows} flow rows")


def stage_bse_identity(con) -> str:
    from engine.universe import builder

    matched = builder.sync_bse_identity(con)
    return f"{matched} securities matched to BSE scrip codes"


def stage_prices(con) -> str:
    """Top up daily bars. Refetches a short overlap to pick up adjustments."""
    from engine.providers.yfinance_provider import YFinanceProvider, classify_ticker
    from engine.universe.builder import investable_universe
    from engine.universe.theme_graph import load_theme_graph
    import pandas as pd

    last = con.execute("SELECT max(date) FROM ohlcv").fetchone()[0]
    start = (last - dt.timedelta(days=10)) if last else dt.date.fromisoformat(settings.HISTORY_START)

    graph = load_theme_graph()
    tickers = dict.fromkeys(graph.all_tickers())
    for ticker in investable_universe(con):
        tickers.setdefault(ticker, None)

    prices = YFinanceProvider().fetch_ohlcv(list(tickers), start=start)
    if prices.empty:
        return "no bars returned"

    seen = prices["ticker"].drop_duplicates()
    profiles = []
    for ticker in seen:
        exchange, country, currency = classify_ticker(ticker)
        profiles.append({
            "ticker": ticker, "exchange_symbol": ticker.split(".")[0], "isin": None,
            "name": None, "exchange": exchange, "country": country,
            "currency": currency, "sector": None, "industry": None,
            "market_cap": None, "listing_date": None, "delisted_date": None,
            "is_active": True, "source": "yfinance",
        })
    db.upsert_securities(con, pd.DataFrame(profiles))

    id_map = db.security_map(con)
    prices = prices.assign(
        security_id=prices["ticker"].map(id_map), source="yfinance"
    ).dropna(subset=["security_id"])
    prices["security_id"] = prices["security_id"].astype("int64")

    written = db.upsert_ohlcv(con, prices[[
        "security_id", "date", "open", "high", "low", "close",
        "adj_close", "volume", "source",
    ]])
    return f"{written:,} bars from {start} across {seen.size} symbols"


def stage_filings_recent(con) -> str:
    """Catch filings published since the last run."""
    from engine.universe import builder

    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=WINDOW_DAYS)
    result = builder.sync_filing_dates(con, start, end)
    return (f"{result['fetched']} announcements {start}..{end}, "
            f"{result['unparsed']} without a parseable period")


def stage_filings_backfill(con, windows: int) -> str:
    """Walk history backwards a few windows per night.

    Bounded on purpose: BSE throttles sustained pagination, so a nightly job
    that tries to do the whole backfill in one go will be cut off and lose the
    run. Two windows a night reaches 2016 in roughly ten months of nights, and
    the recent-window stage keeps current data fresh meanwhile.
    """
    from engine.universe import builder

    cursor = get_cursor(con, BACKFILL_JOB) or (dt.date.today() - dt.timedelta(days=1))
    if cursor <= BACKFILL_FLOOR:
        return f"complete — history reaches {BACKFILL_FLOOR}"

    fetched = 0
    for index in range(windows):
        if cursor <= BACKFILL_FLOOR:
            break
        window_start = max(cursor - dt.timedelta(days=WINDOW_DAYS), BACKFILL_FLOOR)
        result = builder.sync_filing_dates(con, window_start, cursor)
        fetched += result["fetched"]
        cursor = window_start - dt.timedelta(days=1)
        # Advance the cursor after each window, so a throttled run keeps the
        # ground it already covered.
        set_cursor(con, BACKFILL_JOB, cursor, f"last window ended {window_start}")
        if index < windows - 1:
            time.sleep(4.0)   # let BSE's throttle decay between windows

    remaining = max((cursor - BACKFILL_FLOOR).days, 0) // WINDOW_DAYS
    return (f"{fetched} announcements, cursor now {cursor}, "
            f"~{remaining} windows to {BACKFILL_FLOOR}")


def stage_score(con) -> str:
    """Score the universe into `scores`, which the bands and payload read.

    Scoring was never part of the night, so `scores` only moved when someone ran
    `investo score` by hand -- and the band display, which reads that table,
    could sit weeks behind the gate verdicts printed beside it.
    """
    from engine.scoring import gem

    as_of = dt.date.today()
    frame = gem.score_universe(con, as_of=as_of, include_non_pit=True)
    if frame.empty:
        return "nothing to score"

    stored = gem.store_scores(con, frame, as_of)
    return f"{len(stored)} scored, mean coverage {stored['coverage'].mean():.0f}%"


def stage_payload(con) -> str:
    """Recompute theme indices, divergence and the Today payload.

    RUNS LAST, and the order is the point. This stage used to sit before the
    gates, so the payload the app serves was assembled from the PREVIOUS night's
    verdicts -- every screening figure on the Today and Screen tabs was one run
    stale by construction, while looking current. It reads gate_results and
    scores, so it has to follow both.
    """
    import json

    from engine import demo_data

    # Lend the night's own connection. Opening a read-only one here raises
    # "Can't open a connection to same database file with a different
    # configuration" -- DuckDB will not mix configurations within a process --
    # which is why this stage had never once succeeded inside the nightly job.
    payload = demo_data.build(con)
    settings.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (settings.REPORT_DIR / "today.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    trailing = sum(1 for t in payload["themes"]
                   if t["divergence"].get("state") == "INDIA_TRAILING")
    return (f"{len(payload['themes'])} themes as of {payload['as_of']}, "
            f"{trailing} with India trailing")


def stage_thesis_health(con) -> str:
    """Re-check every open position against the night's fresh screen.

    Runs after the gates so a newly failing gate shows up as an AMBER or RED
    holding the next morning, rather than waiting to be noticed.
    """
    from engine.portfolio import book

    folio = book.connect()
    try:
        # Lend the night's analytics connection; opening a read-only one inside
        # this process is what made this stage fail every night.
        health = book.review_thesis(folio, analytics_con=con)
        if health.empty:
            return "no open positions"
        counts = health["health"].value_counts().to_dict()
        return " · ".join(f"{k.lower()} {v}" for k, v in counts.items())
    finally:
        folio.close()


def stage_gates(con) -> str:
    from engine.scoring import gates as gate_engine

    frame = gate_engine.run_gates(con)
    if frame.empty:
        return "nothing to evaluate"
    verdict = gate_engine.verdicts(frame)
    counts = verdict["verdict"].value_counts().to_dict()
    verdict.to_csv(settings.REPORT_DIR / "gate_verdicts.csv", index=False)
    return (f"{counts.get('REJECTED', 0)} rejected, {counts.get('FLAGGED', 0)} flagged, "
            f"{counts.get('UNVETTED', 0)} unvetted, {counts.get('CLEARED', 0)} cleared")


# ----------------------------------------------------------------- entrypoint
def run(windows: int = 3, skip_prices: bool = False) -> NightlyReport:
    report = NightlyReport(started=dt.datetime.now())
    con = db.connect()
    try:
        _stage(report, "universe", lambda: stage_universe(con))
        _stage(report, "bse identity", lambda: stage_bse_identity(con))
        if skip_prices:
            report.stages.append(StageResult("prices", "SKIPPED", "disabled by flag"))
        else:
            _stage(report, "prices", lambda: stage_prices(con))
        _stage(report, "filings recent", lambda: stage_filings_recent(con))
        _stage(report, "filings backfill", lambda: stage_filings_backfill(con, windows))
        # Order matters from here down: gates and scores are inputs to both the
        # thesis review and the payload, so anything that READS them has to run
        # after they are written. The payload build used to sit above the gates,
        # which is how the app came to serve last night's verdicts as today's.
        _stage(report, "gates", lambda: stage_gates(con))
        _stage(report, "score", lambda: stage_score(con))
        _stage(report, "thesis health", lambda: stage_thesis_health(con))
        _stage(report, "today payload", lambda: stage_payload(con))
    finally:
        con.close()

    settings.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = settings.REPORT_DIR / "nightly.log"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(report.render() + "\n\n")

    return report


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        report = run()
    except Exception:  # noqa: BLE001 - last resort, must still leave a trace
        settings.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        with (settings.REPORT_DIR / "nightly.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} FATAL\n")
            handle.write(traceback.format_exc() + "\n")
        raise

    print(report.render())
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
