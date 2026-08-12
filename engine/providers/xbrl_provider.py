"""NSE filings provider -- reported financials with genuine filing dates.

WHY THIS EXISTS
---------------
Yahoo returns statements keyed by period end with no filing date. Using the
period end as a stand-in makes every figure appear public ~45 days before it was,
which inflates backtested returns. NSE's corporate-filings API carries a real
`filingDate` timestamp, so it is the only fundamentals source the engine trusts.

TWO CALLS PER COMPANY
---------------------
1. `fetch_filing_index(symbol)` -- one request, returns every results filing
   the company has made (125 for WABAG), each with period bounds, filing
   timestamp, consolidated flag and a link to the XBRL instance document.
2. `fetch_xbrl(url)` -- the instance document, parsed into facts.

RESTATEMENTS
------------
A company can file the same period twice. WABAG's Oct-Dec 2024 quarter appears
on 07-Feb-2025 and again on 13-Mar-2025. Both rows are kept: `fundamentals_pit`
is append-only and `db.fundamentals_asof` picks the newest filing visible on the
as-of date, so a backtest sees the original figure until the revision was
actually published.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from xml.etree import ElementTree

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
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json,text/xml,*/*",
}

# XBRL tag -> canonical metric. NSE serves BSE-schema documents, so tags follow
# the in-bse-fin taxonomy. Anything unmapped is reported by `unmapped_tags` so
# coverage gaps surface instead of silently becoming zeroes.
TAG_MAP = {
    "RevenueFromOperations": "revenue",
    "OtherIncome": "other_income",
    "Income": "total_income",
    "CostOfMaterialsConsumed": "materials_cost",
    "PurchasesOfStockInTrade": "purchases_stock_in_trade",
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade": "inventory_change",
    "EmployeeBenefitExpense": "employee_cost",
    "FinanceCosts": "finance_cost",
    "DepreciationDepletionAndAmortisationExpense": "depreciation",
    "OtherExpenses": "other_expenses",
    "Expenses": "total_expenses",
    "ProfitBeforeExceptionalItemsAndTax": "pbt_before_exceptional",
    "ExceptionalItemsBeforeTax": "exceptional_items",
    "ProfitBeforeTax": "pbt",
    "CurrentTax": "current_tax",
    "DeferredTax": "deferred_tax",
    "TaxExpense": "tax_expense",
    "ProfitLossForPeriod": "pat",
    "ProfitLossForPeriodFromContinuingOperations": "pat_continuing",
    "ComprehensiveIncomeForThePeriod": "comprehensive_income",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_basic",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_diluted",
    "PaidUpValueOfEquityShareCapital": "equity_capital",
    "RevenueFromInterest": "interest_income",
}

# Facts carrying these dimensions are segment- or category-level breakdowns, not
# company totals. Keeping them would double-count revenue.
DIMENSIONAL_HINT = re.compile(r"explicitMember", re.I)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_date(value) -> dt.date | None:
    # Guards against NaN: pandas hands back a float for a missing date, and
    # `not float('nan')` is False, so a naive truthiness check would fall
    # through to .strip() and raise.
    if value is None or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def classify_period(start: dt.date | None, end: dt.date | None) -> str | None:
    """Quarterly, annual, or a cumulative stub we do not want.

    Indian filings mix single-quarter figures with year-to-date and full-year
    columns in one document. Only clean Q and A periods are stored; anything
    else (a 6- or 9-month cumulative) would corrupt growth maths downstream.
    """
    if not start or not end:
        return None
    days = (end - start).days
    if 80 <= days <= 100:
        return "Q"
    if 350 <= days <= 380:
        return "A"
    return None


class NSEFilingsProvider:
    name = "nse_xbrl"
    supports_fundamentals = True

    def __init__(self) -> None:
        self._session: cr.Session | None = None

    def _api(self) -> cr.Session:
        if self._session is None:
            session = cr.Session(impersonate=BROWSER, headers=HEADERS)
            session.get(WWW, timeout=settings.REQUEST_TIMEOUT)
            time.sleep(0.5)
            self._session = session
        return self._session

    # -------------------------------------------------------- filing index
    def fetch_filing_index(self, symbol: str, period: str = "Quarterly") -> pd.DataFrame:
        """Every results filing this company has made."""
        session = self._api()
        url = (f"{WWW}/api/corporates-financial-results"
               f"?index=equities&symbol={symbol}&period={period}")
        try:
            response = session.get(
                url, timeout=settings.REQUEST_TIMEOUT,
                headers={"Referer": f"{WWW}/companies-listing/corporate-filings-financial-results"},
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"filing index failed for {symbol}: {exc}") from exc

        if response.status_code != 200:
            raise ProviderError(f"filing index HTTP {response.status_code} for {symbol}")

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"filing index not JSON for {symbol}: {exc}") from exc

        rows = payload if isinstance(payload, list) else payload.get("data", [])
        if not rows:
            return pd.DataFrame()

        frame = pd.DataFrame(rows)
        out = pd.DataFrame({
            "symbol": frame.get("symbol"),
            "period_start": frame["fromDate"].map(_parse_date),
            "period_end": frame["toDate"].map(_parse_date),
            "filing_date": frame["filingDate"].map(_parse_date),
            "broadcast_date": frame.get("broadCastDate", pd.Series(dtype="object")).map(_parse_date),
            "consolidated": frame.get("consolidated"),
            "audited": frame.get("audited"),
            "xbrl_url": frame.get("xbrl"),
            "seq": frame.get("seqNumber"),
        })

        # Fall back to broadcast date when filingDate is absent -- both are
        # publication events, so neither leaks future information.
        out["filing_date"] = out["filing_date"].fillna(out["broadcast_date"])
        out = out.dropna(subset=["period_end", "filing_date", "xbrl_url"])
        out["period_type"] = [
            classify_period(s, e) for s, e in zip(out["period_start"], out["period_end"])
        ]
        return out.reset_index(drop=True)

    # ---------------------------------------------------------------- xbrl
    def fetch_xbrl(self, url: str) -> str:
        try:
            response = cr.get(url, headers=HEADERS, impersonate=BROWSER,
                              timeout=settings.REQUEST_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"xbrl fetch failed {url}: {exc}") from exc
        if response.status_code != 200:
            raise ProviderError(f"xbrl HTTP {response.status_code} for {url}")
        return response.text

    @staticmethod
    def parse_xbrl(text: str) -> tuple[pd.DataFrame, set[str]]:
        """Extract company-level facts with their true reporting period.

        Returns (facts, unmapped_tags).

        CRITICAL: the `<context>` element's own startDate is NOT reliable. A
        results document carries both the quarter and the year-to-date column,
        and NSE stamps the *quarter's* start date on both contexts. For WABAG
        Q3 FY25, context `FourD` declares startDate 2024-10-01 while the fact
        `DateOfStartOfReportingPeriod` inside it says 2024-04-01 -- it is the
        nine-month cumulative (Rs 21.38bn), not the quarter (Rs 8.11bn).

        So the period is taken from the declared DateOfStartOfReportingPeriod /
        DateOfEndOfReportingPeriod facts, falling back to the context element
        only when those are absent. Trusting the context element would silently
        mix 3-month and 9-month figures across companies and wreck every growth
        calculation downstream.

        Facts carrying an explicit dimension are dropped: those are segment or
        product breakdowns which would double-count against the total.
        """
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise ProviderError(f"malformed xbrl: {exc}") from exc

        # Pass 1: context element dates (fallback only) and dimensionality.
        ctx_fallback: dict[str, tuple[dt.date | None, dt.date | None]] = {}
        dimensional: set[str] = set()
        for context in root.iter():
            if _localname(context.tag) != "context":
                continue
            cid = context.get("id")
            if not cid:
                continue
            start = end = None
            for node in context.iter():
                local = _localname(node.tag)
                if local == "startDate":
                    start = _parse_date(node.text)
                elif local in ("endDate", "instant"):
                    end = _parse_date(node.text)
                elif local in ("explicitMember", "typedMember"):
                    dimensional.add(cid)
            ctx_fallback[cid] = (start, end)

        # Pass 2: the authoritative period and basis declared per context.
        declared_start: dict[str, dt.date] = {}
        declared_end: dict[str, dt.date] = {}
        basis: dict[str, str] = {}
        for node in root.iter():
            cref = node.get("contextRef")
            if not cref or node.text is None:
                continue
            local = _localname(node.tag)
            if local == "DateOfStartOfReportingPeriod":
                value = _parse_date(node.text)
                if value:
                    declared_start[cref] = value
            elif local == "DateOfEndOfReportingPeriod":
                value = _parse_date(node.text)
                if value:
                    declared_end[cref] = value
            elif local == "NatureOfReportStandaloneConsolidated":
                basis[cref] = node.text.strip()

        # Pass 3: the facts themselves.
        rows = []
        unmapped: set[str] = set()
        for node in root.iter():
            local = _localname(node.tag)
            cref = node.get("contextRef")
            if not cref or node.text is None:
                continue
            text_value = node.text.strip()
            if not text_value:
                continue

            metric = TAG_MAP.get(local)
            if metric is None:
                if cref in ctx_fallback and re.fullmatch(r"-?[\d.]+", text_value):
                    unmapped.add(local)
                continue
            if cref in dimensional:
                continue

            try:
                value = float(text_value)
            except ValueError:
                continue

            fallback_start, fallback_end = ctx_fallback.get(cref, (None, None))
            sign = -1.0 if (node.get("sign") == "-") else 1.0
            rows.append({
                "metric": metric,
                "value": value * sign,
                "period_start": declared_start.get(cref, fallback_start),
                "period_end": declared_end.get(cref, fallback_end),
                "basis": basis.get(cref),
                "context": cref,
                "unit": node.get("unitRef"),
            })

        return pd.DataFrame(rows), unmapped

    # ------------------------------------------------------- orchestration
    def fetch_fundamentals(
        self, symbol: str, max_filings: int = 16, prefer_consolidated: bool = True
    ) -> pd.DataFrame:
        """Long-form facts for one company, ready for `fundamentals_pit`.

        `max_filings` bounds the work: a full history is 125 documents per
        company, which is not worth downloading for a screener that needs about
        four years of quarters.
        """
        index = self.fetch_filing_index(symbol)
        if index.empty:
            return pd.DataFrame()

        index = index[index["period_type"].notna()].copy()
        if index.empty:
            return pd.DataFrame()

        # One filing per (period, basis): keep the newest, since an earlier
        # revision of the same period adds nothing a backtest can use once the
        # later one is visible.
        if prefer_consolidated:
            index["basis_rank"] = (index["consolidated"] != "Consolidated").astype(int)
        else:
            index["basis_rank"] = 0

        index = (
            index.sort_values(["period_end", "basis_rank", "filing_date"],
                              ascending=[False, True, False])
            .drop_duplicates(subset=["period_end", "period_type"], keep="first")
            .head(max_filings)
        )

        frames = []
        for record in index.itertuples():
            try:
                facts, unmapped = self.parse_xbrl(self.fetch_xbrl(record.xbrl_url))
            except ProviderError as exc:
                log.warning("%s %s: %s", symbol, record.period_end, exc)
                continue
            if unmapped:
                log.debug("%s unmapped tags: %s", symbol, sorted(unmapped)[:8])
            if facts.empty:
                continue

            # Keep only facts for this filing's own period. The same document
            # also carries the year-to-date column, which shares this period_end
            # but starts at the financial-year open -- classify_period rejects it
            # because its span is ~275 days rather than a quarter.
            match = facts[facts["period_end"] == record.period_end].copy()
            if match.empty:
                continue
            match = match[
                [classify_period(s, e) == record.period_type
                 for s, e in zip(match["period_start"], match["period_end"])]
            ]

            # A document can carry both bases; keep the one this filing is for
            # so standalone and consolidated figures never interleave.
            wanted = "Consolidated" if record.consolidated == "Consolidated" else "Standalone"
            if match["basis"].notna().any():
                same_basis = match[match["basis"].str.strip() == wanted]
                if not same_basis.empty:
                    match = same_basis
            if match.empty:
                continue

            match["symbol"] = symbol
            match["period_type"] = record.period_type
            match["filing_date"] = record.filing_date
            match["consolidated"] = record.consolidated
            frames.append(match)
            time.sleep(settings.RATE_LIMIT_SLEEP)

        if not frames:
            return pd.DataFrame()

        out = pd.concat(frames, ignore_index=True)
        # Same metric can appear twice in one document; keep the last occurrence.
        return out.drop_duplicates(
            subset=["symbol", "period_end", "period_type", "metric", "filing_date"],
            keep="last",
        )
