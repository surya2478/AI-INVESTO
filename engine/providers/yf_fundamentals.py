"""Yahoo quarterly financials — a free second source for Indian companies.

WHY THIS EXISTS ALONGSIDE PDF EXTRACTION
----------------------------------------
Extracting every statement from PDFs was the wrong default. Yahoo publishes
quarterly income statements for Indian listings at no cost and no latency, and
two independent sources agreeing is stronger evidence than any single
extraction plus arithmetic checks. Where both exist and agree, confidence is
high; where they disagree, something is wrong and neither should be trusted
silently.

WHAT IT CANNOT DO, AND WHY THAT STILL MATTERS
---------------------------------------------
Yahoo reports the CURRENT value of each figure with no indication of when it
became public, and restatements overwrite history in place. That is fine for
screening today and fatal for a backtest: a strategy tested on figures that were
not public at the time will look better than it was. So these rows are stored
with `is_pit = FALSE` and `fundamentals_asof` excludes them by default. They
inform what to buy now; they must never inform what would have worked then.

Measured limits (Aug 2026): about 5-6 quarters of history per company, with
real gaps — Siemens returns nothing for Dec-2024, CG Power skips Sep-2025 — and
Basic EPS comes back as 0.0, so EPS is not mapped. The line items are also
shallow: no expense breakdown, so operating leverage still needs the statement.
"""

from __future__ import annotations

import logging

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# Yahoo row label -> our canonical metric. Deliberately conservative: only
# labels whose meaning is unambiguous are mapped.
ROW_MAP = {
    "Operating Revenue": "revenue",
    "Total Revenue": "revenue",
    "Pretax Income": "pbt",
    "Net Income": "pat",
    "EBITDA": "ebitda",
    "Tax Provision": "tax_expense",
    "Interest Expense": "finance_cost",
    "Reconciled Depreciation": "depreciation",
    "Total Expenses": "total_expenses",
}

# Basic EPS is returned as 0.0 for every Indian ticker checked, so it is
# excluded rather than stored as a wrong number.
EXCLUDED = {"Basic EPS", "Diluted EPS"}


def fetch_quarterly(ticker: str) -> pd.DataFrame:
    """Quarterly income-statement lines for one ticker, long form."""
    try:
        frame = yf.Ticker(ticker).quarterly_income_stmt
    except Exception as exc:  # noqa: BLE001 - a missing company is not fatal
        log.debug("yahoo fundamentals failed for %s: %s", ticker, exc)
        return pd.DataFrame()

    if frame is None or frame.empty:
        return pd.DataFrame()

    rows = []
    for label, metric in ROW_MAP.items():
        if label not in frame.index:
            continue
        for period, value in frame.loc[label].items():
            if pd.isna(value):
                continue
            rows.append({
                "ticker": ticker,
                "period_end": pd.Timestamp(period).date(),
                "period_type": "Q",
                "metric": metric,
                "value": float(value),
                "unit": "INR",
                "source": "yfinance",
            })

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # "Total Revenue" and "Operating Revenue" both map to revenue; keep one.
    return out.drop_duplicates(subset=["ticker", "period_end", "metric"], keep="first")


def cross_check(
    extracted: dict, reference: dict, tolerance: float = 0.02
) -> tuple[int, int, list[str]]:
    """Compare an extraction against Yahoo for the same period.

    Returns (agreed, compared, disagreements). A wider tolerance than the
    XBRL comparison (2% vs 0.5%) because the two sources genuinely differ on
    standalone-versus-consolidated basis and on how EBITDA is reconciled — the
    check is for gross error, not for reconciliation.
    """
    agreed = compared = 0
    problems: list[str] = []

    for metric, value in extracted.items():
        other = reference.get(metric)
        if other is None or value is None:
            continue
        if not isinstance(value, (int, float)) or not isinstance(other, (int, float)):
            continue
        scale = max(abs(value), abs(other), 1.0)
        compared += 1
        if abs(value - other) / scale <= tolerance:
            agreed += 1
        else:
            problems.append(f"{metric}: extracted {value:,.0f} vs yahoo {other:,.0f}")

    return agreed, compared, problems


def fetch_annual(ticker: str) -> pd.DataFrame:
    """Annual income-statement lines — 4-5 years, enough for growth acceleration.

    Quarterly history only reaches back 5-6 quarters, which cannot express a
    2-year versus 4-year CAGR comparison. The annual series can.
    """
    try:
        frame = yf.Ticker(ticker).income_stmt
    except Exception as exc:  # noqa: BLE001
        log.debug("yahoo annual failed for %s: %s", ticker, exc)
        return pd.DataFrame()

    if frame is None or frame.empty:
        return pd.DataFrame()

    rows = []
    for label, metric in ROW_MAP.items():
        if label not in frame.index:
            continue
        for period, value in frame.loc[label].items():
            if pd.isna(value):
                continue
            rows.append({
                "ticker": ticker,
                "period_end": pd.Timestamp(period).date(),
                "period_type": "A",
                "metric": metric,
                "value": float(value),
                "unit": "INR",
                "source": "yfinance",
            })

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.drop_duplicates(subset=["ticker", "period_end", "metric"], keep="first")


# Cash-flow lines. ANNUAL ONLY -- Yahoo returns an empty quarterly cash-flow
# frame for every Indian ticker checked, which is consistent with Indian
# companies filing cash flows half-yearly at best.
CASHFLOW_MAP = {
    "Operating Cash Flow": "cfo",
    "Free Cash Flow": "fcf",
    "Capital Expenditure": "capex",
}


def fetch_cashflow(ticker: str) -> pd.DataFrame:
    """Annual cash-flow lines — the input to the cash-conversion gate.

    Cumulative operating cash flow against cumulative reported profit is the
    single best accounting-fraud filter available from public data: profit is an
    opinion, cash is a fact, and a company that reports years of profit without
    generating cash is either growing very working-capital intensively or not
    really earning it.
    """
    try:
        frame = yf.Ticker(ticker).cashflow
    except Exception as exc:  # noqa: BLE001
        log.debug("yahoo cashflow failed for %s: %s", ticker, exc)
        return pd.DataFrame()

    if frame is None or frame.empty:
        return pd.DataFrame()

    rows = []
    for label, metric in CASHFLOW_MAP.items():
        if label not in frame.index:
            continue
        for period, value in frame.loc[label].items():
            if pd.isna(value):
                continue
            rows.append({
                "ticker": ticker,
                "period_end": pd.Timestamp(period).date(),
                "period_type": "A",
                "metric": metric,
                "value": float(value),
                "unit": "INR",
                "source": "yfinance",
            })

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.drop_duplicates(subset=["ticker", "period_end", "metric"], keep="first")


def fetch_all(ticker: str) -> pd.DataFrame:
    """Quarterly income, annual income and annual cash-flow lines."""
    frames = [
        f for f in (fetch_quarterly(ticker), fetch_annual(ticker), fetch_cashflow(ticker))
        if not f.empty
    ]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def coverage_probe(tickers: list[str], limit: int = 25) -> pd.DataFrame:
    """How much of a universe Yahoo actually covers, before relying on it."""
    rows = []
    for ticker in tickers[:limit]:
        frame = fetch_quarterly(ticker)
        if frame.empty:
            rows.append({"ticker": ticker, "quarters": 0, "metrics": 0,
                         "earliest": None, "latest": None})
            continue
        rows.append({
            "ticker": ticker,
            "quarters": frame["period_end"].nunique(),
            "metrics": frame["metric"].nunique(),
            "earliest": frame["period_end"].min(),
            "latest": frame["period_end"].max(),
        })
    return pd.DataFrame(rows)
