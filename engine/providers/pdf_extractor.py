"""Extract quarterly financials from BSE result PDFs via the Claude API.

WHY THIS EXISTS
---------------
Structured filings (NSE/BSE XBRL) stop at the Dec-2024 quarter. From 2025 both
exchanges publish results under SEBI's Integrated Filing framework as signed
PDFs with no machine-readable companion. BSE bundles them per quarter as a ZIP
containing separately-named standalone and consolidated statements, which makes
it the better extraction source than NSE's scattered attachments.

THE TRAP THIS PROMPT IS BUILT AROUND
------------------------------------
Indian results carry three columns for the same period end: the quarter, the
year-to-date cumulative, and the prior-year comparative. The XBRL parser hit
exactly this and silently returned nine-month figures as quarterly ones (WABAG
Q3FY25: Rs 21.38bn cumulative vs Rs 8.11bn for the quarter). The same trap
exists in the PDFs, so the prompt states the expected period explicitly and the
schema requires the model to echo back the period it actually read — which the
caller then verifies rather than trusts.

ACCURACY IS MEASURED, NOT ASSUMED
---------------------------------
`scripts/validate_pdf_extraction.py` runs this over quarters where XBRL is
already stored and reports a per-metric error rate. Nothing extracted here
feeds a score until that comparison passes.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import logging
import re
import zipfile
from typing import Literal

import pandas as pd
from curl_cffi import requests as cr
from pydantic import BaseModel, Field

from engine.config import settings
from engine.providers.base import ProviderError

log = logging.getLogger(__name__)

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_WWW = "https://www.bseindia.com"
BROWSER = "chrome"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{BSE_WWW}/",
    "Origin": BSE_WWW,
}

# Indian statements state their unit in a header line. Getting this wrong scales
# every figure by 100x, so the model reports it and we convert explicitly.
UNIT_TO_RUPEES = {
    "rupees": 1.0,
    "thousands": 1e3,
    "lakhs": 1e5,
    "millions": 1e6,
    "crores": 1e7,
    "billions": 1e9,
}


class QuarterlyFinancials(BaseModel):
    """One reporting period from one results statement."""

    period_start: str = Field(description="Start of the period THIS column covers, YYYY-MM-DD")
    period_end: str = Field(description="End of the period THIS column covers, YYYY-MM-DD")
    basis: Literal["Consolidated", "Standalone"]
    amounts_in: Literal["rupees", "thousands", "lakhs", "millions", "crores", "billions"] = Field(
        description="The unit the statement declares, e.g. '(Rs. in lakhs)'"
    )
    audited: bool | None = Field(default=None, description="True if audited, False if unaudited")

    revenue: float | None = Field(default=None, description="Revenue from operations")
    other_income: float | None = None
    total_income: float | None = None
    materials_cost: float | None = Field(default=None, description="Cost of materials consumed")
    employee_cost: float | None = Field(default=None, description="Employee benefit expense")
    finance_cost: float | None = None
    depreciation: float | None = Field(default=None, description="Depreciation and amortisation")
    other_expenses: float | None = None
    total_expenses: float | None = None
    exceptional_items: float | None = None
    pbt: float | None = Field(default=None, description="Profit before tax")
    tax_expense: float | None = Field(default=None, description="Total tax expense")
    pat: float | None = Field(default=None, description="Profit after tax for the period")
    eps_basic: float | None = Field(default=None, description="Basic EPS in rupees, NOT scaled by amounts_in")
    eps_diluted: float | None = Field(default=None, description="Diluted EPS in rupees, NOT scaled by amounts_in")

    extraction_notes: str | None = Field(
        default=None,
        description="Anything ambiguous, illegible, or absent. Empty if the statement was clean.",
    )


# EPS is quoted per share in rupees regardless of the statement's unit header.
UNSCALED_METRICS = {"eps_basic", "eps_diluted"}

SYSTEM_PROMPT = """\
You extract line items from Indian listed-company quarterly results statements.

The single most important rule concerns WHICH COLUMN to read. These statements
present the same row across several columns: the quarter just ended, the
year-to-date cumulative (six or nine months), and comparative columns for the
prior quarter and the prior year. They share a period end, so the column header
is the only thing that distinguishes them.

Read ONLY the column matching the period the user names. A column headed
"Nine months ended 31 December 2024" is NOT the quarter ended 31 December 2024.
If you cannot find a column for exactly that period, set every figure to null
and say so in extraction_notes rather than substituting a nearby column.

Further rules:
- Report figures exactly as printed. Do not convert units, and report the
  statement's declared unit in amounts_in.
- EPS is per share in rupees; report it as printed and do not scale it.
- A figure in parentheses is negative.
- Report expenses as positive numbers, matching how the statement prints them.
- Use null for any line the statement does not report. Never infer, derive, or
  compute a missing value from other lines.
"""


class BSEPDFProvider:
    """Fetch BSE result bundles and extract financials from the PDFs inside."""

    name = "bse_pdf"

    def __init__(self, model: str = "claude-opus-5") -> None:
        self.model = model
        self._session: cr.Session | None = None
        self._client = None

    # ------------------------------------------------------------- transport
    def _bse(self) -> cr.Session:
        if self._session is None:
            session = cr.Session(impersonate=BROWSER, headers=HEADERS)
            session.get(f"{BSE_WWW}/", timeout=settings.REQUEST_TIMEOUT)
            self._session = session
        return self._session

    def _anthropic(self):
        """Create the Claude client lazily, with an actionable error if unset."""
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ProviderError("pip install anthropic") from exc
            try:
                self._client = anthropic.Anthropic()
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(
                    "No Anthropic credentials found. Set ANTHROPIC_API_KEY in "
                    "C:\\AI-Investo\\.env, or run `ant auth login`."
                ) from exc
        return self._client

    # ------------------------------------------------------- bundle discovery
    def fetch_result_bundles(self, scripcode: str) -> pd.DataFrame:
        """Per-quarter result ZIPs a company has filed, newest financial year first."""
        session = self._bse()
        response = session.get(
            f"{BSE_API}/FinancialResult/w?scripcode={scripcode}&type=Q",
            timeout=settings.REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise ProviderError(f"BSE FinancialResult HTTP {response.status_code}")

        try:
            html = response.json()["Data"]
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"unexpected FinancialResult payload: {exc}") from exc

        rows = []
        for row_html in re.findall(r"<tr>(.*?)</tr>", html, re.S):
            year = re.search(r"(\d{4}-\d{4})", row_html)
            if not year:
                continue
            # Columns run Q1..Q4 then the annual column, in that order.
            links = re.findall(r'href=[\'"]?(/downloads1/[^\'">\s]+\.zip)', row_html)
            for index, href in enumerate(links):
                rows.append({
                    "financial_year": year.group(1),
                    "column_index": index,
                    "quarter": f"Q{index + 1}" if index < 4 else "FY",
                    "zip_url": f"{BSE_WWW}{href}",
                })

        return pd.DataFrame(rows)

    def fetch_statement_pdf(
        self, zip_url: str, prefer_consolidated: bool = True
    ) -> tuple[bytes, str]:
        """Pull the financial-results PDF out of a quarter's bundle.

        Filenames are NOT standardised across quarters or companies — observed
        forms include "BSE CO FR Jun'2026_Signed.pdf" and
        "website_Q3_2024_25/BSE_Consolidated_Q3.pdf". So candidates are scored
        rather than pattern-matched, and anything that is clearly not a
        statement (investor presentation, exchange intimation, board-meeting
        outcome) is excluded outright.
        """
        session = self._bse()
        response = session.get(zip_url, timeout=120)
        if response.status_code != 200:
            raise ProviderError(f"bundle HTTP {response.status_code} for {zip_url}")

        try:
            bundle = zipfile.ZipFile(io.BytesIO(response.content))
        except zipfile.BadZipFile as exc:
            raise ProviderError(f"not a zip: {zip_url}") from exc

        excluded = (
            "presentation", "intimation", "outcome", "board meeting", "newspaper",
            "press release", "transcript", "recording", "advertisement", "certificate",
        )
        wanted = "consolidated" if prefer_consolidated else "standalone"
        other = "standalone" if prefer_consolidated else "consolidated"

        def score(name: str) -> int:
            base = name.rsplit("/", 1)[-1].lower()
            if not base.endswith(".pdf") or any(word in base for word in excluded):
                return -1
            points = 1
            # Long-form names, then the abbreviated CO/SA token forms.
            if wanted in base:
                points += 10
            elif f"_{wanted[:2]}_" in base or f" {wanted[:2].upper()} " in name:
                points += 8
            if other in base or f"_{other[:2]}_" in base:
                points += 2      # still a statement, just the other basis
            if "fr" in base.replace("_", " ").split() or "result" in base:
                points += 3
            return points

        ranked = sorted(
            ((score(n), n) for n in bundle.namelist()), key=lambda pair: pair[0], reverse=True
        )
        best_score, chosen = ranked[0] if ranked else (-1, None)
        if chosen is None or best_score < 0:
            raise ProviderError(
                f"no results statement in bundle; members: {bundle.namelist()}"
            )
        return bundle.read(chosen), chosen

    # ------------------------------------------------------------ extraction
    def extract(
        self,
        pdf_bytes: bytes,
        period_start: dt.date,
        period_end: dt.date,
        basis: str = "Consolidated",
    ) -> QuarterlyFinancials:
        """Read one period's figures out of a results PDF."""
        if len(pdf_bytes) > 30 * 1024 * 1024:
            raise ProviderError(f"pdf too large for one request: {len(pdf_bytes):,} bytes")

        client = self._anthropic()
        encoded = base64.standard_b64encode(pdf_bytes).decode("ascii")

        instruction = (
            f"Extract the {basis.lower()} figures for the quarter that starts "
            f"{period_start:%d %B %Y} and ends {period_end:%d %B %Y} — a three-month "
            f"period.\n\n"
            f"Do NOT read the year-to-date column, which also ends {period_end:%d %B %Y} "
            f"but starts at the beginning of the financial year. Confirm the column you "
            f"used by returning its period in period_start and period_end."
        )

        response = client.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            output_format=QuarterlyFinancials,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": encoded,
                        },
                    },
                    {"type": "text", "text": instruction},
                ],
            }],
        )

        if response.stop_reason == "refusal":
            raise ProviderError(
                "Claude declined this document "
                f"({getattr(response.stop_details, 'category', 'unknown')})"
            )
        if response.parsed_output is None:
            raise ProviderError(f"no structured output (stop_reason={response.stop_reason})")

        return response.parsed_output

    # ------------------------------------------------------------ conversion
    @staticmethod
    def to_facts(
        extracted: QuarterlyFinancials,
        symbol: str,
        filing_date: dt.date,
        expected_end: dt.date,
    ) -> pd.DataFrame:
        """Normalise an extraction to rupees, in `fundamentals_pit` shape.

        Verifies the model read the period it was asked for. A mismatch means it
        used the wrong column, and the extraction is discarded rather than
        stored — silently keeping it is how nine-month figures end up scored as
        quarterly ones.
        """
        got_end = dt.date.fromisoformat(extracted.period_end)
        got_start = dt.date.fromisoformat(extracted.period_start)
        span = (got_end - got_start).days

        if got_end != expected_end:
            raise ProviderError(
                f"{symbol}: model read period ending {got_end}, expected {expected_end}"
            )
        if not 80 <= span <= 100:
            raise ProviderError(
                f"{symbol}: model returned a {span}-day period, not a quarter "
                f"({got_start} to {got_end}) — likely the cumulative column"
            )

        multiplier = UNIT_TO_RUPEES[extracted.amounts_in]
        rows = []
        for metric, value in extracted.model_dump().items():
            if value is None or metric in {
                "period_start", "period_end", "basis", "amounts_in",
                "audited", "extraction_notes",
            }:
                continue
            scale = 1.0 if metric in UNSCALED_METRICS else multiplier
            rows.append({
                "symbol": symbol,
                "period_end": got_end,
                "period_type": "Q",
                "filing_date": filing_date,
                "metric": metric,
                "value": float(value) * scale,
                "unit": "INR",
                "basis": extracted.basis,
            })

        return pd.DataFrame(rows)
