"""Ingest reported financials into `fundamentals_pit`.

Append-only by design. A company that restates a period files again with a later
`filing_date`; both rows persist and `db.fundamentals_asof` resolves which one a
given as-of date could have seen. Nothing here ever updates a stored figure.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from engine.providers.base import ProviderError
from engine.providers.xbrl_provider import NSEFilingsProvider
from engine.storage import db

log = logging.getLogger(__name__)

# Derived from reported lines rather than taken on trust: Indian filings report
# EBITDA inconsistently, if at all, but every one of these inputs is mandatory.
def derive_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    """Add EBITDA and margin rows computed from reported components."""
    if frame.empty:
        return frame

    wide = frame.pivot_table(
        index=["symbol", "period_end", "period_type", "filing_date"],
        columns="metric", values="value", aggfunc="last",
    )

    derived = []
    if {"pbt", "finance_cost", "depreciation"} <= set(wide.columns):
        ebitda = wide["pbt"] + wide["finance_cost"] + wide["depreciation"]
        # Other income is not operating; strip it so margins reflect the business.
        if "other_income" in wide.columns:
            ebitda = ebitda - wide["other_income"].fillna(0)
        derived.append(ebitda.rename("ebitda"))

    if {"revenue"} <= set(wide.columns) and derived:
        margin = (derived[0] / wide["revenue"].replace(0, pd.NA)) * 100
        derived.append(margin.rename("ebitda_margin"))

    # Net worth: attributable to owners where the filing splits out minority
    # interests, since ROE pairs it with profit attributable to owners. Falling
    # back to total equity, then to the components, because which of the three a
    # company reports varies by filing format rather than by anything meaningful.
    if "equity_owners" in wide.columns:
        net_worth = wide["equity_owners"]
    elif "equity_total" in wide.columns:
        net_worth = wide["equity_total"]
    elif {"equity_share_capital", "other_equity"} <= set(wide.columns):
        net_worth = wide["equity_share_capital"] + wide["other_equity"]
    else:
        net_worth = None
    if net_worth is not None:
        derived.append(net_worth.rename("net_worth"))

    # Total borrowings. Either leg may legitimately be absent -- a debt-free
    # company files one line as zero and omits the other -- so a missing leg is
    # treated as zero, but only when at least one was actually reported.
    debt_legs = [c for c in ("borrowings_current", "borrowings_noncurrent") if c in wide.columns]
    if debt_legs:
        total_debt = sum(wide[c].fillna(0) for c in debt_legs)
        derived.append(total_debt.where(wide[debt_legs].notna().any(axis=1)).rename("total_debt"))

    # Share count from paid-up capital and face value. Never from the rupee
    # figure alone: a face-value split moves it without a share being issued.
    #
    # Two sources of paid-up capital, and the fallback is the useful one. The
    # balance-sheet line only appears in annual filings from FY2023, while
    # `equity_capital` is on the face of every quarterly result back to FY2018.
    # Point-in-time market cap needs a share count at every ranking date, so
    # taking it from the quarterly filings is what makes the historical cap
    # computable at all.
    capital = None
    if "equity_share_capital" in wide.columns:
        capital = wide["equity_share_capital"]
        if "equity_capital" in wide.columns:
            capital = capital.fillna(wide["equity_capital"])
    elif "equity_capital" in wide.columns:
        capital = wide["equity_capital"]
    if capital is not None and "face_value" in wide.columns:
        face = wide["face_value"].where(wide["face_value"] > 0)
        derived.append((capital / face).rename("share_count"))

    if not derived:
        return frame

    extra = (
        pd.concat(derived, axis=1)
        .reset_index()
        .melt(id_vars=["symbol", "period_end", "period_type", "filing_date"],
              var_name="metric", value_name="value")
        .dropna(subset=["value"])
    )
    extra["unit"] = None
    extra["basis"] = None
    return pd.concat([frame, extra], ignore_index=True)


def sync_fundamentals(
    con,
    symbols: list[str],
    max_filings: int = 12,
    provider: NSEFilingsProvider | None = None,
    max_annual: int = 12,
) -> dict:
    """Fetch and store quarterly AND annual financials for `symbols`.

    `symbols` are NSE codes without the .NS suffix. `max_annual` governs how far
    back the annual history reaches; twelve years is the horizon a decile
    backtest needs to say anything, and each year is one extra HTTP fetch.
    """
    provider = provider or NSEFilingsProvider()
    id_map = db.security_map(con)

    written = 0
    ok, empty, failed = [], [], []

    for n, symbol in enumerate(symbols, 1):
        security_id = id_map.get(f"{symbol}.NS")
        if security_id is None:
            failed.append((symbol, "not in securities"))
            continue

        try:
            facts = provider.fetch_fundamentals(symbol, max_filings=max_filings,
                                                max_annual=max_annual)
        except ProviderError as exc:
            failed.append((symbol, str(exc)[:80]))
            continue
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the run
            failed.append((symbol, f"{type(exc).__name__}: {exc}"[:80]))
            continue

        if facts.empty:
            empty.append(symbol)
            continue

        facts = derive_metrics(facts)

        staged = pd.DataFrame({
            "security_id": security_id,
            "period_end": facts["period_end"],
            "period_type": facts["period_type"],
            "filing_date": facts["filing_date"],
            "metric": facts["metric"],
            "value": pd.to_numeric(facts["value"], errors="coerce"),
            "unit": facts.get("unit"),
            "source": "nse_xbrl",
        }).dropna(subset=["value", "period_end", "filing_date"])

        staged = staged.drop_duplicates(
            subset=["security_id", "period_end", "period_type", "metric", "filing_date"],
            keep="last",
        )
        if staged.empty:
            empty.append(symbol)
            continue

        con.register("staged_fund", staged)
        con.execute("""
            INSERT INTO fundamentals_pit
                (security_id, period_end, period_type, filing_date, metric, value, unit, source)
            SELECT s.security_id, s.period_end, s.period_type, s.filing_date,
                   s.metric, s.value, s.unit, s.source
              FROM staged_fund s
             WHERE NOT EXISTS (
                   SELECT 1 FROM fundamentals_pit f
                    WHERE f.security_id = s.security_id
                      AND f.period_end  = s.period_end
                      AND f.period_type = s.period_type
                      AND f.metric      = s.metric
                      AND f.filing_date = s.filing_date
             )
        """)
        con.unregister("staged_fund")

        written += len(staged)
        ok.append(symbol)
        if n % 25 == 0:
            log.info("fundamentals: %d/%d symbols", n, len(symbols))

    return {"written": written, "ok": ok, "empty": empty, "failed": failed}


def sync_yahoo_fundamentals(con, tickers: list[str], progress=None) -> dict:
    """Load Yahoo quarterly and annual figures as the primary screening source.

    Stored with `is_pit = FALSE`: Yahoo reports the current value of each figure
    and overwrites restatements in place, so there is no honest filing date to
    record. `filing_date` is set to the period end purely to satisfy the primary
    key -- it is NOT a publication date, which is exactly why these rows are
    fenced off from as-of reads.
    """
    from engine.providers.yf_fundamentals import fetch_all

    id_map = db.security_map(con)
    written = 0
    ok, empty = [], []

    for index, ticker in enumerate(tickers, 1):
        security_id = id_map.get(ticker)
        if security_id is None:
            empty.append(ticker)
            continue

        frame = fetch_all(ticker)
        if frame.empty:
            empty.append(ticker)
            if progress:
                progress(index, len(tickers), ticker, 0)
            continue

        staged = pd.DataFrame({
            "security_id": security_id,
            "period_end": frame["period_end"],
            "period_type": frame["period_type"],
            # Not a publication date. See docstring.
            "filing_date": frame["period_end"],
            "metric": frame["metric"],
            "value": pd.to_numeric(frame["value"], errors="coerce"),
            "unit": frame["unit"],
            "source": "yfinance",
            "is_pit": False,
        }).dropna(subset=["value"])

        if staged.empty:
            empty.append(ticker)
            continue

        con.register("staged_yf", staged)
        con.execute("""
            INSERT INTO fundamentals_pit
                (security_id, period_end, period_type, filing_date, metric,
                 value, unit, source, is_pit)
            SELECT s.security_id, s.period_end, s.period_type, s.filing_date,
                   s.metric, s.value, s.unit, s.source, s.is_pit
              FROM staged_yf s
             WHERE NOT EXISTS (
                   SELECT 1 FROM fundamentals_pit f
                    WHERE f.security_id = s.security_id AND f.period_end = s.period_end
                      AND f.period_type = s.period_type AND f.metric = s.metric
                      AND f.filing_date = s.filing_date
             )
        """)
        con.unregister("staged_yf")

        written += len(staged)
        ok.append(ticker)
        if progress:
            progress(index, len(tickers), ticker, len(staged))

    return {"written": written, "ok": len(ok), "empty": len(empty),
            "missing": empty[:20]}


def coverage_report(con) -> pd.DataFrame:
    """Per-metric coverage, so gaps are visible before anything is scored."""
    return con.execute("""
        SELECT metric,
               count(DISTINCT security_id) AS companies,
               count(*)                    AS facts,
               min(period_end)             AS earliest,
               max(period_end)             AS latest
          FROM fundamentals_pit
         GROUP BY metric
         ORDER BY companies DESC, metric
    """).df()


def pit_selftest(con, as_of: dt.date | None = None) -> dict:
    """Prove the point-in-time guard actually blocks unpublished figures.

    Counts facts whose filing_date is after the as-of date, then confirms
    `fundamentals_asof` returns none of them. A backtest that silently reads
    future filings would show an edge that does not exist, so this runs as a
    test rather than a comment.

    The default as-of is the midpoint of the stored filing range, which
    guarantees there ARE facts on the far side of it. A fixed date could sit
    past every filing we hold, and the test would pass while proving nothing.
    """
    if as_of is None:
        bounds = con.execute(
            "SELECT min(filing_date), max(filing_date) FROM fundamentals_pit"
        ).fetchone()
        if bounds and bounds[0] and bounds[1]:
            lo, hi = pd.Timestamp(bounds[0]), pd.Timestamp(bounds[1])
            as_of = (lo + (hi - lo) / 2).date()
        else:
            as_of = dt.date.today() - dt.timedelta(days=180)

    future = con.execute(
        "SELECT count(*) FROM fundamentals_pit WHERE filing_date > ?", [as_of]
    ).fetchone()[0]

    visible = db.fundamentals_asof(con, as_of)
    # DuckDB hands back datetime64; compare like with like rather than date-vs-timestamp.
    leaked = 0
    if not visible.empty:
        filed = pd.to_datetime(visible["filing_date"])
        leaked = int((filed > pd.Timestamp(as_of)).sum())

    return {
        "as_of": as_of,
        "facts_filed_after": int(future),
        "rows_visible": 0 if visible.empty else len(visible),
        "leaked": leaked,
        "passed": leaked == 0,
    }


def ownership_dating_report(con) -> dict:
    """Whether ownership rows are dated by publication or by the scraper's clock.

    A whole table sharing ONE filing_date is the signature of a feed stamped
    with the ingest time rather than the disclosure time. That is not a leak --
    an ingest date is later than the real filing, so it hides data rather than
    revealing it early -- but it hid ALL of it: promoter holding and pledge were
    invisible at every historical date, and 15% of the quality pillar's weight
    had never been populated in any backtest that ran.

    Nothing here rewrites the data, because whether a given date was a genuine
    broadcast cannot be recovered after the fact. It reports the shape so the
    question gets asked.
    """
    row = con.execute("""
        SELECT count(*)                        AS rows,
               count(DISTINCT filing_date)     AS distinct_filing_dates,
               count(DISTINCT quarter_end)     AS quarters,
               min(filing_date)                AS earliest,
               max(filing_date)                AS latest,
               sum(CASE WHEN coalesce(is_pit, TRUE) THEN 0 ELSE 1 END) AS inferred_dates,
               sum(CASE WHEN filing_date < quarter_end THEN 1 ELSE 0 END) AS impossible_dates
          FROM ownership_pit
    """).df().to_dict("records")[0]

    total = int(row["rows"] or 0)
    distinct = int(row["distinct_filing_dates"] or 0)
    row["single_date_for_whole_table"] = bool(total > 1 and distinct == 1)
    row["passed"] = not row["single_date_for_whole_table"] and not int(row["impossible_dates"] or 0)
    return row
