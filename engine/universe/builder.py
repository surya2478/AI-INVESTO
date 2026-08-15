"""Build the investable universe from NSE's own records.

Design shift from Stage 0: the universe is the whole NSE market (~750 names via
NIFTY TOTAL MARKET, plus microcap), and the hand-authored theme graph is a
*tagging layer* on top. Otherwise the screener could only ever find companies I
had already thought to list, which defeats the purpose of a discovery tool.

SURVIVORSHIP CAVEAT
-------------------
NSE publishes only current constituents and current listings -- there is no free
archive of historical index membership or delisted companies. So:

  * `index_membership.from_date` is the first date WE observed a name in the
    index, not the date it actually joined. Dated membership becomes genuinely
    accurate only from this run forward.
  * A 2015-2026 backtest built on today's membership inherits survivorship bias:
    companies that failed and delisted are simply absent, which flatters returns.

Stage 3 must correct for this or state the bias plainly in its output. It is a
data-availability limit, not something more engineering here can fix.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd

from engine.config import settings

from engine.providers.base import ProviderError
from engine.providers.nse_provider import INDEX_FILES, NSEProvider
from engine.storage import db

log = logging.getLogger(__name__)


def sync_symbol_master(con, provider: NSEProvider) -> int:
    """Load every NSE-listed equity into `securities`."""
    master = provider.fetch_symbol_master()
    if master.empty:
        return 0

    staged = pd.DataFrame({
        "ticker": master["ticker"],
        "exchange_symbol": master["exchange_symbol"],
        "isin": master["isin"],
        "name": master["name"],
        "exchange": "NSE",
        "country": "IN",
        "currency": "INR",
        "sector": None,
        "industry": None,
        "market_cap": None,
        "listing_date": master["listing_date"],
        "delisted_date": None,
        "is_active": True,
        "source": provider.name,
    })
    return db.upsert_securities(con, staged)


def sync_index_membership(con, provider: NSEProvider) -> dict[str, int]:
    """Record current index constituents, opening and closing membership spans."""
    today = dt.date.today()
    members = provider.fetch_all_index_constituents()
    if members.empty:
        return {}

    # Industry labels from the constituent files are better than anything
    # yfinance returns for Indian names, so fold them into securities.
    industries = (
        members.dropna(subset=["industry"])
        .drop_duplicates("ticker")[["ticker", "industry"]]
    )
    con.register("staged_industry", industries)
    con.execute("""
        UPDATE securities s
           SET industry = i.industry, updated_at = current_timestamp
          FROM staged_industry i
         WHERE s.ticker = i.ticker AND s.industry IS NULL
    """)
    con.unregister("staged_industry")

    id_map = db.security_map(con)
    members = members.assign(security_id=members["ticker"].map(id_map))
    unresolved = int(members["security_id"].isna().sum())
    if unresolved:
        log.warning("%d constituents not in symbol master (likely non-EQ series)",
                    unresolved)
    members = members.dropna(subset=["security_id"])
    members["security_id"] = members["security_id"].astype("int64")

    counts: dict[str, int] = {}
    for index_name in INDEX_FILES:
        current = members.loc[members["index_name"] == index_name, ["security_id"]]
        if current.empty:
            continue
        counts[index_name] = len(current)

        con.register("staged_members", current)

        # Open a span for names not currently marked as members.
        con.execute("""
            INSERT INTO index_membership (index_name, security_id, from_date, to_date, source)
            SELECT ?, m.security_id, ?, NULL, 'nse'
              FROM staged_members m
             WHERE NOT EXISTS (
                   SELECT 1 FROM index_membership im
                    WHERE im.index_name = ?
                      AND im.security_id = m.security_id
                      AND im.to_date IS NULL
             )
        """, [index_name, today, index_name])

        # Close spans for names that have dropped out since the last run.
        con.execute("""
            UPDATE index_membership
               SET to_date = ?
             WHERE index_name = ?
               AND to_date IS NULL
               AND security_id NOT IN (SELECT security_id FROM staged_members)
        """, [today, index_name])

        con.unregister("staged_members")

    return counts


def sync_surveillance(con, provider: NSEProvider) -> int:
    """Record ASM-flagged names as corporate events for the quality gates."""
    asm = provider.fetch_asm_list()
    if asm.empty:
        return 0

    id_map = db.security_map(con)
    asm = asm.assign(security_id=asm["ticker"].map(id_map)).dropna(subset=["security_id"])
    if asm.empty:
        return 0

    staged = pd.DataFrame({
        "security_id": asm["security_id"].astype("int64"),
        "event_date": asm["event_date"].fillna(dt.date.today()),
        "event_type": "ASM_SURVEILLANCE",
        "detail": asm["bucket"].str.upper() + " " + asm["stage"].fillna(""),
        "severity": "CRITICAL",
        "source": "nse",
    })

    # A name can appear in both the long-term and short-term buckets on the same
    # day, which collides on the primary key. Keep one row and merge the labels
    # so neither listing is lost.
    staged = (
        staged.groupby(["security_id", "event_date", "event_type"], as_index=False)
        .agg({"detail": lambda s: "; ".join(sorted(set(s))),
              "severity": "first", "source": "first"})
    )

    con.register("staged_events", staged)
    con.execute("""
        INSERT INTO corporate_events
            (security_id, event_date, event_type, detail, severity, source)
        SELECT s.security_id, s.event_date, s.event_type, s.detail, s.severity, s.source
          FROM staged_events s
         WHERE NOT EXISTS (
               SELECT 1 FROM corporate_events e
                WHERE e.security_id = s.security_id
                  AND e.event_date  = s.event_date
                  AND e.event_type  = s.event_type
         )
    """)
    con.unregister("staged_events")
    return len(staged)


def sync_index_levels(con, provider: NSEProvider) -> int:
    levels = provider.fetch_all_indices()
    if levels.empty:
        return 0
    con.register("staged_levels", levels)
    con.execute("""
        INSERT INTO index_levels
            (index_name, date, last, open, high, low, previous_close, pct_change, source)
        SELECT l.index_name, l.date, l.last, l.open, l.high, l.low,
               l.previous_close, l.pct_change, l.source
          FROM staged_levels l
         WHERE NOT EXISTS (
               SELECT 1 FROM index_levels e
                WHERE e.index_name = l.index_name AND e.date = l.date
         )
    """)
    con.unregister("staged_levels")
    return len(levels)


def sync_flows(con, provider: NSEProvider) -> int:
    flows = provider.fetch_fii_dii()
    if flows.empty:
        return 0
    con.register("staged_flows", flows)
    con.execute("""
        INSERT INTO flows (date, category, buy_value, sell_value, net_value, source)
        SELECT f.date, f.category, f.buy_value, f.sell_value, f.net_value, f.source
          FROM staged_flows f
         WHERE NOT EXISTS (
               SELECT 1 FROM flows e
                WHERE e.date = f.date AND e.category = f.category
         )
    """)
    con.unregister("staged_flows")
    return len(flows)


def sync_bse_identity(con, provider=None) -> int:
    """Attach BSE scrip codes and market caps to securities, joined on ISIN.

    ISIN is the only safe join: BSE scrip codes cannot be inferred from an NSE
    symbol, and guessing one silently attaches another company's financials.
    """
    from engine.providers.bse_provider import BSEProvider

    provider = provider or BSEProvider()
    master = provider.fetch_scrip_master()
    if master.empty:
        return 0

    con.register("staged_bse", master)
    con.execute("""
        UPDATE securities s
           SET bse_scripcode = b.scripcode,
               market_cap    = coalesce(b.market_cap, s.market_cap),
               updated_at    = current_timestamp
          FROM staged_bse b
         WHERE s.isin = b.isin
    """)
    matched = con.execute("""
        SELECT count(*) FROM securities s
          JOIN staged_bse b ON s.isin = b.isin
    """).fetchone()[0]
    con.unregister("staged_bse")
    return int(matched)


def sync_filing_dates(con, start: dt.date, end: dt.date, provider=None) -> dict:
    """Record when result announcements were disseminated, per company and period."""
    from engine.providers.bse_provider import BSEProvider

    provider = provider or BSEProvider()
    announcements = provider.fetch_result_announcements_range(start, end)
    if announcements.empty:
        return {"fetched": 0, "stored": 0, "unparsed": 0}

    unparsed = int(announcements["period_end"].isna().sum())
    usable = announcements.dropna(subset=["period_end"]).copy()
    if usable.empty:
        return {"fetched": len(announcements), "stored": 0, "unparsed": unparsed}

    con.register("staged_ann", usable)
    con.execute("""
        INSERT INTO filing_events
            (security_id, period_end, filing_date, filing_ts, event_type, subject, source)
        SELECT s.security_id, a.period_end, a.filing_date, a.filing_ts,
               'RESULT', a.subject, 'bse'
          FROM staged_ann a
          JOIN securities s ON s.bse_scripcode = a.scripcode
         WHERE NOT EXISTS (
               SELECT 1 FROM filing_events e
                WHERE e.security_id = s.security_id
                  AND e.period_end  = a.period_end
                  AND e.filing_date = a.filing_date
         )
    """)
    stored = con.execute("SELECT count(*) FROM filing_events").fetchone()[0]
    con.unregister("staged_ann")

    return {"fetched": len(announcements), "stored": int(stored), "unparsed": unparsed}


# SEBI LODR Regulation 31: the shareholding pattern is due within 21 days of the
# quarter end. Used only when the exchange gives no publication date of its own.
OWNERSHIP_FILING_LAG_DAYS = 21


def ownership_filing_date(quarter_end: dt.date,
                          reported: dt.date | None) -> tuple[dt.date, bool]:
    """When a shareholding pattern became public, and whether that is known.

    Returns (filing_date, is_pit).

    The old fallback dated an undated disclosure to the QUARTER END itself,
    which asserts the pattern was public on the last day of the quarter it
    describes. It is filed up to 21 days later, so that made ownership visible
    about three weeks before it existed -- a leak in the same family as reading
    today's market cap at a historical date, and pointing the same unsafe way.

    Inferred dates use the statutory deadline, which is LATER than most real
    filings and therefore conservative, and are marked `is_pit = False` so a
    backtest can exclude them rather than trust a date nobody reported.

    A reported date before the quarter even ended is not a filing date -- a
    company cannot disclose a quarter's shareholding before that quarter closes
    -- so it is rejected rather than believed.
    """
    deadline = quarter_end + dt.timedelta(days=OWNERSHIP_FILING_LAG_DAYS)
    if reported is None or reported < quarter_end:
        if reported is not None:
            log.debug("ownership filing date %s precedes quarter end %s; using deadline",
                      reported, quarter_end)
        return deadline, False
    return reported, True


def sync_promoter_pledge(con, tickers: list[str], provider=None, progress=None) -> dict:
    """Load promoter holding and encumbrance for the given tickers."""
    from engine.providers.nse_provider import NSEProvider

    provider = provider or NSEProvider()
    id_map = db.security_map(con)

    rows, missing, failed = [], 0, 0
    for index, ticker in enumerate(tickers, 1):
        security_id = id_map.get(ticker)
        if security_id is None:
            missing += 1
            continue
        symbol = ticker.removesuffix(".NS")
        try:
            record = provider.fetch_promoter_pledge(symbol)
        except ProviderError as exc:
            # Network and API problems are expected and per-company.
            log.debug("pledge unavailable for %s: %s", symbol, exc)
            failed += 1
            record = None
        except Exception as exc:  # noqa: BLE001
            # A code bug is NOT a data problem. Swallowing everything here once
            # hid a NameError behind "0 with disclosure" for the whole universe,
            # which looked like NSE had no data rather than a broken call.
            log.exception("pledge code error on %s", symbol)
            raise RuntimeError(f"pledge extraction is broken: {exc}") from exc

        if record and record.get("quarter_end"):
            filing_date, is_pit = ownership_filing_date(
                record["quarter_end"], record.get("filing_date")
            )
            promoter_pct = record.get("promoter_pct")
            rows.append({
                "security_id": security_id,
                "quarter_end": record["quarter_end"],
                "filing_date": filing_date,
                "is_pit": is_pit,
                "promoter_pct": promoter_pct,
                "promoter_pledge_pct": record.get("promoter_pledge_pct"),
                "fii_pct": None, "dii_pct": None,
                # A percentage, not the share count NSE reports under
                # `totPublicHolding` -- which is what used to land here, so the
                # column held figures like 687,313,904 in a field called _pct.
                "public_pct": (100.0 - promoter_pct) if promoter_pct is not None else None,
                "source": "nse",
            })
        else:
            missing += 1
        if progress:
            progress(index, len(tickers), symbol, bool(record))
        time.sleep(settings.RATE_LIMIT_SLEEP)

    if not rows:
        return {"written": 0, "missing": missing, "failed": failed}

    staged = pd.DataFrame(rows)
    con.register("staged_pledge", staged)
    con.execute("""
        DELETE FROM ownership_pit
         WHERE (security_id, quarter_end, filing_date) IN (
               SELECT security_id, quarter_end, filing_date FROM staged_pledge)
    """)
    con.execute("""
        INSERT INTO ownership_pit
            (security_id, quarter_end, filing_date, is_pit, promoter_pct,
             promoter_pledge_pct, fii_pct, dii_pct, public_pct, source)
        SELECT security_id, quarter_end, filing_date, is_pit, promoter_pct,
               promoter_pledge_pct, fii_pct, dii_pct, public_pct, source
          FROM staged_pledge
    """)
    con.unregister("staged_pledge")

    return {"written": len(staged), "missing": missing, "failed": failed}


def sync_shareholding_history(con, tickers: list[str], provider=None,
                              progress=None) -> dict:
    """Backfill every shareholding pattern a company has filed.

    APPEND-ONLY, unlike `sync_promoter_pledge`, which deletes and rewrites. A
    filing that has been published cannot un-publish, and a revision arrives as
    a new row with a later `filing_date` -- exactly the discipline
    `fundamentals_pit` uses, so the same as-of read resolves both.

    Rows carry `is_pit = True` because every date here is a broadcast timestamp
    NSE recorded, not a deadline this code assumed.

    Pledge is not in this feed, so `promoter_pledge_pct` is left NULL. Where the
    pledge feed has already written a figure for the same quarter it keeps its
    own row and, being stamped later, still wins the as-of read.
    """
    from engine.providers.nse_provider import NSEProvider

    provider = provider or NSEProvider()
    id_map = db.security_map(con)

    rows, missing, failed = [], 0, 0
    for index, ticker in enumerate(tickers, 1):
        security_id = id_map.get(ticker)
        if security_id is None:
            missing += 1
            continue
        symbol = ticker.removesuffix(".NS")
        try:
            history = provider.fetch_shareholding_history(symbol)
        except ProviderError as exc:
            log.debug("shareholding unavailable for %s: %s", symbol, exc)
            failed += 1
            history = []
        except Exception as exc:  # noqa: BLE001
            # A code bug is not a data problem; the pledge path learned this the
            # hard way when a NameError read as "no disclosure" for a universe.
            log.exception("shareholding code error on %s", symbol)
            raise RuntimeError(f"shareholding extraction is broken: {exc}") from exc

        if not history:
            missing += 1
        for record in history:
            filing_date, is_pit = ownership_filing_date(
                record["quarter_end"], record.get("filing_date")
            )
            rows.append({
                "security_id": security_id,
                "quarter_end": record["quarter_end"],
                "filing_date": filing_date,
                "is_pit": is_pit,
                "promoter_pct": record.get("promoter_pct"),
                "promoter_pledge_pct": None,
                "fii_pct": None, "dii_pct": None,
                "public_pct": record.get("public_pct"),
                "source": "nse_shp",
            })
        if progress:
            progress(index, len(tickers), symbol, len(history))
        time.sleep(settings.RATE_LIMIT_SLEEP)

    if not rows:
        return {"written": 0, "companies": 0, "missing": missing, "failed": failed}

    staged = pd.DataFrame(rows).drop_duplicates(
        subset=["security_id", "quarter_end", "filing_date"], keep="last")
    con.register("staged_shp", staged)
    con.execute("""
        INSERT INTO ownership_pit
            (security_id, quarter_end, filing_date, is_pit, promoter_pct,
             promoter_pledge_pct, fii_pct, dii_pct, public_pct, source)
        SELECT s.security_id, s.quarter_end, s.filing_date, s.is_pit, s.promoter_pct,
               s.promoter_pledge_pct, s.fii_pct, s.dii_pct, s.public_pct, s.source
          FROM staged_shp s
         WHERE NOT EXISTS (
               SELECT 1 FROM ownership_pit o
                WHERE o.security_id = s.security_id
                  AND o.quarter_end = s.quarter_end
                  AND o.filing_date = s.filing_date
         )
    """)
    con.unregister("staged_shp")

    return {"written": len(staged), "companies": staged.security_id.nunique(),
            "missing": missing, "failed": failed}


def investable_universe(
    con, index_name: str = "NIFTY TOTAL MARKET", include_microcap: bool = True
) -> list[str]:
    """Tickers the scoring engine should consider, as of today."""
    indices = [index_name]
    if include_microcap:
        indices.append("NIFTY MICROCAP 250")

    placeholders = ",".join("?" * len(indices))
    rows = con.execute(f"""
        SELECT DISTINCT s.ticker
          FROM index_membership im
          JOIN securities s ON s.security_id = im.security_id
         WHERE im.index_name IN ({placeholders})
           AND im.to_date IS NULL
         ORDER BY s.ticker
    """, indices).fetchall()
    return [r[0] for r in rows]
