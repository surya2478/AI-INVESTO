"""Order inflow and order book, from NSE announcement text.

WHY THIS MATTERS
----------------
For capital goods, defence, EPC and water — most of the themes here — order book
to sales is the strongest forward signal available. Revenue tells you what a
company already did; the order book tells you what it is contracted to do next.
Nothing else in this engine sees it.

FREE FIRST
----------
Announcement text carries the number often enough to be worth parsing before
paying anyone to read a PDF:

    "BEL receives order worth Rs.1081 Crore"                       -> inflow
    "WABAG records all-time high Rs. 194 Bn order book in Q1 FY27" -> book

but not always:

    "Kalpataru ... has informed the Exchange about Bagging/Receiving
     of orders/contracts"                                          -> nothing

Companies that publish only boilerplate need the attachment read, which costs
money. `unpriced_events` lists them so that spend can be aimed at the companies
where it actually buys something.

INFLOW IS NOT BACKLOG
---------------------
An individual contract win adds to the book; the book is the outstanding total.
Summing wins does not give you the book, because execution burns it down. The
two are stored as separate event kinds and never added together.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time

import pandas as pd
from curl_cffi import requests as cr

from engine.config import settings
from engine.providers.base import ProviderError

log = logging.getLogger(__name__)

WWW = "https://www.nseindia.com"
BROWSER = "chrome"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Announcement categories that carry order news.
ORDER_DESC = re.compile(
    r"bagging|receiv(ing|ed) of order|awarding of order|order.*contract|press release",
    re.I,
)

# Everything normalised to Rs CRORE. Indian releases mix crore, billion, million
# and lakh freely, sometimes within one sentence.
UNIT_TO_CRORE = {
    "cr": 1.0, "crore": 1.0, "crores": 1.0,
    "bn": 100.0, "billion": 100.0, "billions": 100.0,
    "mn": 0.1, "million": 0.1, "millions": 0.1,
    "lakh": 0.01, "lakhs": 0.01, "lac": 0.01,
}

_AMOUNT = re.compile(
    r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*"
    r"(cr(?:ore)?s?|bn|billions?|mn|millions?|lakhs?|lac)\b",
    re.I,
)

# "order book", "orderbook", "order backlog" -> a total, not a single win.
_BOOK_CONTEXT = re.compile(r"order\s*book|orderbook|order\s*backlog|total\s+order", re.I)
# Phrases that mean the number is not an order value at all.
_NOT_ORDER = re.compile(
    r"penalt|fine of|demand of|tax|gst|dividend|buyback|allotment|"
    r"resignation|rating|dispute",
    re.I,
)


def parse_amounts(text: str) -> list[tuple[float, str]]:
    """All rupee amounts in a string, normalised to crore."""
    out = []
    for raw, unit in _AMOUNT.findall(text or ""):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        multiplier = UNIT_TO_CRORE.get(unit.lower().rstrip("s"), None)
        if multiplier is None:
            multiplier = UNIT_TO_CRORE.get(unit.lower())
        if multiplier:
            out.append((value * multiplier, unit.lower()))
    return out


def classify(text: str) -> str | None:
    """ORDER_BOOK (a total) or ORDER_WIN (one contract), or None."""
    if not text or _NOT_ORDER.search(text):
        return None
    if _BOOK_CONTEXT.search(text):
        return "ORDER_BOOK"
    if re.search(r"order|contract|bagg|award|secure[sd]?\b|wins?\b", text, re.I):
        return "ORDER_WIN"
    return None


def _session(symbol: str) -> cr.Session:
    session = cr.Session(impersonate=BROWSER, headers={
        **HEADERS, "Referer": f"{WWW}/get-quotes/equity?symbol={symbol}"})
    session.get(WWW, timeout=settings.REQUEST_TIMEOUT)
    return session


def fetch_order_events(symbol: str, since: dt.date | None = None) -> pd.DataFrame:
    """Order-related announcements for one company, with any amount parsed out."""
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            session = _session(symbol)
            response = session.get(
                f"{WWW}/api/corporate-announcements?index=equities&symbol={symbol}",
                timeout=90)
            if response.status_code == 200:
                break
            last = ProviderError(f"HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - retried
            last = exc
        time.sleep(attempt * 2.0)
    else:
        raise ProviderError(f"announcements failed for {symbol}: {last}")

    try:
        rows = response.json() or []
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"announcements not JSON for {symbol}: {exc}") from exc

    out = []
    for record in rows:
        desc = record.get("desc") or ""
        text = f"{desc}. {record.get('attchmntText') or ''}"
        if not ORDER_DESC.search(desc) and not ORDER_DESC.search(text):
            continue

        kind = classify(text)
        if kind is None:
            continue

        announced = _parse_dt(record.get("an_dt"))
        if announced is None or (since and announced < since):
            continue

        amounts = parse_amounts(text)
        # Largest amount in the text: releases often quote a headline figure
        # alongside smaller components of the same contract.
        value = max((a for a, _ in amounts), default=None)

        out.append({
            "symbol": symbol,
            "event_date": announced,
            "kind": kind,
            "value_cr": value,
            "has_amount": value is not None,
            "headline": text.strip()[:400],
            "pdf_url": record.get("attchmntFile"),
        })

    return pd.DataFrame(out)


def _parse_dt(value) -> dt.date | None:
    if not isinstance(value, str):
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def summarise(events: pd.DataFrame, as_of: dt.date | None = None) -> dict:
    """Latest disclosed book, and trailing twelve-month win inflow.

    Kept separate on purpose. The book is a stock, wins are a flow, and adding
    them would double-count work already in the backlog.
    """
    as_of = as_of or dt.date.today()
    if events.empty:
        return {"order_book_cr": None, "inflow_12m_cr": None,
                "wins_12m": 0, "unpriced_12m": 0}

    priced = events[events["value_cr"].notna()]

    books = priced[priced["kind"] == "ORDER_BOOK"].sort_values("event_date")
    latest_book = float(books["value_cr"].iloc[-1]) if not books.empty else None
    book_date = books["event_date"].iloc[-1] if not books.empty else None

    window = as_of - dt.timedelta(days=365)
    recent = events[events["event_date"] >= window]
    recent_wins = recent[recent["kind"] == "ORDER_WIN"]
    priced_wins = recent_wins[recent_wins["value_cr"].notna()]

    return {
        "order_book_cr": latest_book,
        "order_book_as_of": book_date,
        "inflow_12m_cr": float(priced_wins["value_cr"].sum()) if not priced_wins.empty else None,
        "wins_12m": int(len(recent_wins)),
        # The honest denominator: how much of the last year's news carried no
        # number, and so is missing from the inflow figure.
        "unpriced_12m": int(len(recent_wins) - len(priced_wins)),
    }
