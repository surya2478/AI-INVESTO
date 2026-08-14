"""Ingest order events and derive book-to-sales.

Book-to-sales is the point of all this: an order book worth two years of revenue
says something revenue alone cannot, and for EPC, defence and capital goods it is
the closest thing to forward visibility available from public disclosure.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from engine.providers.orderbook import fetch_order_events, summarise
from engine.storage import db

log = logging.getLogger(__name__)


def sync_orders(con, tickers: list[str], progress=None) -> dict:
    """Fetch and store order events for the given tickers."""
    id_map = db.security_map(con)
    stored = 0
    with_book = missing = 0

    for index, ticker in enumerate(tickers, 1):
        security_id = id_map.get(ticker)
        if security_id is None:
            missing += 1
            continue
        symbol = ticker.removesuffix(".NS")

        try:
            events = fetch_order_events(symbol)
        except Exception as exc:  # noqa: BLE001 - one company must not stop the run
            log.debug("orders failed for %s: %s", symbol, exc)
            if progress:
                progress(index, len(tickers), symbol, 0)
            continue

        if events.empty:
            if progress:
                progress(index, len(tickers), symbol, 0)
            continue

        staged = pd.DataFrame({
            "security_id": security_id,
            "event_date": events["event_date"],
            "kind": events["kind"],
            "value_cr": events["value_cr"],
            "headline": events["headline"],
            "pdf_url": events["pdf_url"],
            "source": "nse_announcements",
        }).drop_duplicates(subset=["security_id", "event_date", "kind", "headline"])

        con.register("staged_orders", staged)
        con.execute("""
            INSERT INTO order_events
                (security_id, event_date, kind, value_cr, headline, pdf_url, source)
            SELECT s.security_id, s.event_date, s.kind, s.value_cr, s.headline,
                   s.pdf_url, s.source
              FROM staged_orders s
             WHERE NOT EXISTS (
                   SELECT 1 FROM order_events e
                    WHERE e.security_id = s.security_id AND e.event_date = s.event_date
                      AND e.kind = s.kind AND e.headline = s.headline)
        """)
        con.unregister("staged_orders")

        stored += len(staged)
        if (events["kind"] == "ORDER_BOOK").any() and events["value_cr"].notna().any():
            with_book += 1
        if progress:
            progress(index, len(tickers), symbol, len(staged))

    return {"stored": stored, "with_book": with_book, "missing": missing}


# An order book disclosure ages badly: it is a snapshot of backlog on a date,
# and the book is consumed by execution. Companies disclose it with results, so
# anything older than about a year and a quarter is not current. The first run
# surfaced a Siemens figure from 2012 and a Genus one from 2011 against 2026
# revenue, which is not a ratio, it is a category error.
MAX_BOOK_AGE_DAYS = 450


def enrich_unpriced(con, limit: int = 50, since_days: int = 540,
                    model: str | None = None, progress=None) -> dict:
    """Read the attachments of order events whose text carried no figure.

    Aimed narrowly on purpose. Only events that are recent enough to matter,
    have an attachment, and are still missing a value are sent — reading a
    document that already yielded its number from the free text would be paying
    for something we have.

    Extracted values are written back with source 'pdf_llm' so the two tiers
    stay distinguishable, and foreign-currency orders are recorded WITHOUT a
    rupee figure rather than converted at a guessed rate.
    """
    from engine.providers.pdf_extractor import (
        BSEPDFProvider,
        InsufficientCredit,
        order_value_in_crore,
    )

    provider = BSEPDFProvider(model=model)
    floor = dt.date.today() - dt.timedelta(days=since_days)

    targets = con.execute("""
        SELECT o.security_id, s.ticker, o.event_date, o.kind, o.headline, o.pdf_url
          FROM order_events o JOIN securities s ON s.security_id = o.security_id
         WHERE o.value_cr IS NULL AND o.pdf_url IS NOT NULL
           AND o.pdf_url LIKE '%.pdf' AND o.event_date >= ?
         ORDER BY o.event_date DESC
         LIMIT ?
    """, [floor, limit]).df()

    if targets.empty:
        return {"attempted": 0, "priced": 0, "cost": 0.0,
                "message": "nothing unpriced and recent enough to be worth reading"}

    priced = neither = failed = foreign = 0
    cost = 0.0
    aborted = None

    for index, record in enumerate(targets.itertuples(), 1):
        try:
            pdf = provider.fetch_pdf(record.pdf_url)
            disclosure, usage = provider.extract_order(
                pdf, filename=record.pdf_url.rsplit("/", 1)[-1])
            cost += usage.get("cost_usd") or 0.0
        except InsufficientCredit as exc:
            aborted = str(exc)
            break
        except Exception as exc:  # noqa: BLE001 - one document must not end the run
            log.debug("order pdf failed for %s: %s", record.ticker, exc)
            failed += 1
            if progress:
                progress(index, len(targets), record.ticker, None)
            continue

        value = order_value_in_crore(disclosure)
        if disclosure.kind == "NEITHER":
            neither += 1
        elif value is None and disclosure.value is not None:
            foreign += 1

        if value is not None:
            con.execute("""
                UPDATE order_events
                   SET value_cr = ?, kind = ?, source = 'pdf_llm'
                 WHERE security_id = ? AND event_date = ? AND headline = ?
            """, [value, disclosure.kind, record.security_id,
                  record.event_date, record.headline])
            priced += 1

        if progress:
            progress(index, len(targets), record.ticker, value)

    return {"attempted": len(targets), "priced": priced, "neither": neither,
            "foreign_currency": foreign, "failed": failed, "cost": cost,
            "aborted": aborted}


def book_to_sales(con, as_of: dt.date | None = None,
                  max_age_days: int = MAX_BOOK_AGE_DAYS) -> pd.DataFrame:
    """Recently disclosed order book against trailing revenue.

    Reported in YEARS of revenue, because that is how the number is read: a book
    worth 2.5 years of sales is strong visibility, 0.4 years is not, and the
    ratio travels across companies of very different size.

    Stale disclosures are dropped rather than shown with a caveat — a number
    that looks like forward visibility but describes a decade ago is worse than
    no number.
    """
    as_of = as_of or dt.date.today()
    floor = as_of - dt.timedelta(days=max_age_days)

    books = con.execute("""
        WITH latest AS (
            SELECT security_id, max(event_date) AS event_date
              FROM order_events
             WHERE kind = 'ORDER_BOOK' AND value_cr IS NOT NULL
               AND event_date <= ? AND event_date >= ?
             GROUP BY security_id)
        SELECT l.security_id, l.event_date, max(o.value_cr) AS order_book_cr
          FROM latest l
          JOIN order_events o ON o.security_id = l.security_id
           AND o.event_date = l.event_date AND o.kind = 'ORDER_BOOK'
         GROUP BY l.security_id, l.event_date
    """, [as_of, floor]).df()
    if books.empty:
        return books

    revenue = con.execute("""
        WITH recent AS (
            SELECT security_id, period_end, value,
                   row_number() OVER (PARTITION BY security_id ORDER BY period_end DESC) AS rn
              FROM fundamentals_pit
             WHERE metric = 'revenue' AND period_type = 'A' AND period_end <= ?)
        SELECT security_id, value AS revenue_inr FROM recent WHERE rn = 1
    """, [as_of]).df()

    profile = con.execute("SELECT security_id, ticker FROM securities").df()

    out = (books.merge(revenue, on="security_id", how="left")
                .merge(profile, on="security_id", how="left"))
    out["revenue_cr"] = out["revenue_inr"] / 1e7
    out["book_to_sales"] = (out["order_book_cr"] / out["revenue_cr"]).where(
        out["revenue_cr"] > 0)
    return out.sort_values("book_to_sales", ascending=False)


def company_orders(con, ticker: str, as_of: dt.date | None = None) -> dict:
    """Order summary for one company, for the app's company sheet."""
    as_of = as_of or dt.date.today()
    events = con.execute("""
        SELECT o.event_date, o.kind, o.value_cr, o.headline
          FROM order_events o JOIN securities s ON s.security_id = o.security_id
         WHERE s.ticker = ? AND o.event_date <= ?
         ORDER BY o.event_date DESC
    """, [ticker, as_of]).df()
    if events.empty:
        return {"available": False}

    events["event_date"] = pd.to_datetime(events["event_date"]).dt.date
    summary = summarise(events.assign(symbol=ticker), as_of)

    revenue = con.execute("""
        SELECT value FROM fundamentals_pit f JOIN securities s ON s.security_id = f.security_id
         WHERE s.ticker = ? AND f.metric = 'revenue' AND f.period_type = 'A'
         ORDER BY f.period_end DESC LIMIT 1
    """, [ticker]).fetchone()
    revenue_cr = (revenue[0] / 1e7) if revenue and revenue[0] else None

    summary["available"] = True
    summary["revenue_cr"] = revenue_cr
    summary["book_to_sales"] = (
        summary["order_book_cr"] / revenue_cr
        if summary.get("order_book_cr") and revenue_cr else None
    )
    summary["recent"] = [
        {"date": str(r.event_date), "kind": r.kind,
         "value_cr": None if pd.isna(r.value_cr) else float(r.value_cr),
         "headline": r.headline[:200]}
        for r in events.head(6).itertuples()
    ]
    return summary
