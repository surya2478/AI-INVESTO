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

import pandas as pd

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
