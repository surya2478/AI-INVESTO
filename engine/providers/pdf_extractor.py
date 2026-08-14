"""Extract quarterly financials from BSE result PDFs via OpenRouter.

Routed through OpenRouter rather than a single vendor, so the model is a config
value (`INVESTO_LLM_MODEL`) instead of a code change. That matters here because
extraction accuracy and cost trade off sharply -- 152 OpenRouter models support
both PDF input and structured outputs, spanning roughly 20x in price -- and
`scripts/validate_pdf_extraction.py` can measure any of them against the XBRL
ground truth before one is trusted.

Two OpenRouter specifics worth knowing:

  * PDF engine defaults to `mistral-ocr` at $2/1,000 pages. BSE statements are
    digital text, not scans, so the engine is pinned to `native` and the model
    reads the document directly. Paying to OCR machine-generated text would be
    waste and would flatten the table structure the figures live in.
  * `provider.require_parameters` is set so a request is refused rather than
    routed to an endpoint that ignores the JSON schema. An unvalidated blob
    that looks like an answer is worse than a clear failure.


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
import json
import logging
import os
import re
import zipfile
from typing import Literal

import pandas as pd
from curl_cffi import requests as cr
from pydantic import BaseModel, Field, ValidationError, field_validator

from engine.config import settings
from engine.providers.base import ProviderError
from engine.providers.bse_provider import parse_period_end

log = logging.getLogger(__name__)

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_WWW = "https://www.bseindia.com"
NSE_WWW = "https://www.nseindia.com"
BROWSER = "chrome"

# Announcement types that carry the numbers. "Newspaper Publication" reprints
# the same figures days later and is excluded by ranking, not by this filter.
TOOL_NAME = "record_financials"

class InsufficientCredit(ProviderError):
    """The account cannot pay for the request. Stop the run, do not record failures."""


RESULT_ANNOUNCEMENT = re.compile(
    r"financial result|integrated filing|outcome of board meeting|unaudited result"
    r"|audited result|quarterly result",
    re.I,
)
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

    @field_validator("audited", mode="before")
    @classmethod
    def _coerce_audited(cls, value):
        """Accept the wording the statements actually print.

        Indian results head this column "Audited" / "Unaudited" / "Un-audited",
        and models return that string rather than a boolean. Rejecting it would
        measure schema compliance instead of extraction accuracy, on a field
        that is metadata and never compared.
        """
        if isinstance(value, str):
            text = value.strip().lower().replace("-", "").replace(" ", "")
            if text in {"audited", "true", "yes"}:
                return True
            if text in {"unaudited", "false", "no"}:
                return False
            return None
        return value

    @field_validator("amounts_in", mode="before")
    @classmethod
    def _coerce_unit(cls, value):
        """Map a printed unit header onto the enum, e.g. 'Rs. in Lakhs'."""
        if isinstance(value, str):
            text = value.strip().lower()
            for unit in ("billions", "crores", "millions", "lakhs", "thousands", "rupees"):
                if unit[:-1] in text:      # singular or plural
                    return unit
        return value

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
    current_tax: float | None = Field(default=None, description="Current tax charge only")
    deferred_tax: float | None = Field(default=None, description="Deferred tax charge only")
    tax_expense: float | None = Field(
        default=None,
        description="Total tax expense. If the statement shows only current and "
                    "deferred components, report both above and leave this null.",
    )
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
- revenue is "Revenue from operations" ONLY. Do not add other income to it, and
  do not report total income in its place.
- EPS is for the SAME three-month column as everything else, never the
  year-to-date or full-year EPS.
- CONTINUING VS TOTAL OPERATIONS. Where a company reports discontinued
  operations, the statement shows profit for continuing operations AND a total
  for the period. Always report the TOTAL — pat and EPS must cover continuing
  plus discontinued. Reading the continuing-operations line alone is the single
  most common error on these statements, and it produces a figure that is
  internally consistent and still wrong. If the statement separates them, say so
  in extraction_notes.
- If the statement shows only current and deferred tax, report those two and
  leave tax_expense null; do not add them up yourself.
- other_expenses is the single line labelled "Other expenses". If the statement
  splits it into several named lines, report their sum.

OUTPUT FORMAT: reply with the JSON object and nothing else. No preamble, no
commentary, no markdown code fences, no notes after it. Put anything you would
have said in prose into the extraction_notes field instead.
"""


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema tightened to what strict JSON-schema mode requires.

    Providers enforcing strict mode need every property listed in `required`,
    `additionalProperties: false` on each object, and no `default` keys. Pydantic
    emits none of that for fields with defaults, so it is normalised here.
    Making optional fields required-but-nullable is deliberate: the model must
    state a null rather than omit the line, which keeps "not reported"
    distinguishable from "not answered".
    """
    schema = model.model_json_schema()

    def tighten(node: dict) -> dict:
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
            for child in node.get("properties", {}).values():
                tighten(child)
        node.pop("default", None)
        for key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(key, []):
                tighten(child)
        for child in (node.get("$defs") or {}).values():
            tighten(child)
        return node

    return tighten(schema)


EXPENSE_COMPONENTS = (
    "materials_cost", "purchases_stock_in_trade", "inventory_change",
    "employee_cost", "finance_cost", "depreciation",
)


def reconcile(values: dict) -> dict:
    """Derive the lines that transcription gets wrong, from ones it gets right.

    Two metrics were consistently misread by BOTH models tested, for structural
    reasons rather than model weakness:

    * `other_expenses` -- statements split it into sub-lines (project costs,
      services, power and fuel), and a reader naturally picks one. XBRL treats
      it as the residual, and its components sum exactly to total_expenses.
      Recomputing it as that residual matches the reference by construction.
    * `tax_expense` -- statements often print only current and deferred tax.
      One model summed them, the other reported current only. Summing is
      arithmetic, so it should not be delegated.

    Derived values are used ONLY where the inputs are present; nothing is
    invented from a partial statement.
    """
    out = dict(values)

    if out.get("tax_expense") is None:
        current, deferred = out.get("current_tax"), out.get("deferred_tax")
        if current is not None or deferred is not None:
            out["tax_expense"] = (current or 0.0) + (deferred or 0.0)

    # NOT derived: other_expenses. Recomputing it as total_expenses minus the
    # other components was tried and measured, and it made accuracy worse
    # (Sonnet 93.3% -> 90.0%): TD Power came out negative, Siemens roughly
    # doubled. The PDF's component set does not match XBRL's, so the residual
    # absorbs whatever the statement itemised separately.
    #
    # The two sources simply define the line differently -- XBRL treats it as a
    # residual, the statement prints a named line -- and neither is wrong. It is
    # excluded from the accuracy comparison for that reason, and nothing
    # downstream consumes it: EBITDA derives from pbt, finance_cost,
    # depreciation and other_income.

    return out


def consistency_check(values: dict, tolerance: float = 0.01) -> list[str]:
    """Arithmetic identities a results statement must satisfy.

    This is the guard that does not need ground truth. A statement where
    revenue + other income does not equal total income has been misread, and
    that is detectable at extraction time for every company -- including the
    thousands we will never have XBRL for.
    """
    problems = []

    def close(a, b, label):
        if a is None or b is None:
            return
        scale = max(abs(a), abs(b), 1.0)
        if abs(a - b) / scale > tolerance:
            problems.append(f"{label}: {a:,.0f} vs {b:,.0f}")

    revenue, other_income = values.get("revenue"), values.get("other_income")
    if revenue is not None and other_income is not None:
        close(values.get("total_income"), revenue + other_income, "total_income != revenue + other_income")

    components = [values.get(k) for k in EXPENSE_COMPONENTS] + [values.get("other_expenses")]
    if all(c is not None for c in components):
        close(values.get("total_expenses"), sum(components), "total_expenses != sum(components)")

    income, expenses = values.get("total_income"), values.get("total_expenses")
    if income is not None and expenses is not None:
        expected = income - expenses + (values.get("exceptional_items") or 0.0)
        close(values.get("pbt"), expected, "pbt != income - expenses")

    pbt, tax = values.get("pbt"), values.get("tax_expense")
    if pbt is not None and tax is not None:
        close(values.get("pat"), pbt - tax, "pat != pbt - tax")

    return problems


def _flexible_date(value) -> dt.date | None:
    """Parse a date the model echoed back, in whatever form it chose.

    The schema asks for YYYY-MM-DD and most models comply, but not all: Qwen
    returns "31 December 2024". Since this value exists purely to verify the
    model read the right column, rejecting it on formatting would discard a
    correct extraction over punctuation.
    """
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
                "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def adjacent_column_check(
    values: dict, prior_values: dict, threshold: float = 0.6
) -> str | None:
    """Detect an extraction that actually read the comparative column.

    The worst failure on these statements is not a garbled number, it is a
    perfectly clean read of the wrong column. TD Power's Dec-2024 extraction
    returned the September quarter's figures exactly: internally consistent, so
    `consistency_check` passed it, and the model still echoed back the correct
    period, so the period guard passed too.

    The one thing that does give it away is that the numbers equal the PREVIOUS
    quarter's. That is checkable against data we already store, needs no
    reference source and no stronger model.

    Returns a description when the extraction looks like a duplicate of the
    prior quarter, else None.
    """
    if not prior_values:
        return None

    comparable = matches = 0
    for metric, value in values.items():
        prior = prior_values.get(metric)
        if value is None or prior is None or not isinstance(value, (int, float)):
            continue
        if abs(prior) < 1.0:                      # zeros match trivially
            continue
        comparable += 1
        if abs(value - prior) / abs(prior) <= 0.005:
            matches += 1

    if comparable >= 5 and matches / comparable >= threshold:
        return (f"{matches}/{comparable} metrics identical to the prior quarter — "
                "likely read the comparative column")
    return None


# Phrases that only appear on a results statement page, used to find the few
# pages worth sending out of a bundled board-meeting packet.
STATEMENT_MARKERS = (
    "revenue from operations", "profit before tax", "total income",
    "profit for the period", "earnings per share", "total expenses",
)


def trim_to_statement(pdf_bytes: bytes, max_pages: int = 6) -> tuple[bytes, str]:
    """Cut a bundled filing down to the pages holding the results statement.

    Adani Green and others publish ONLY a 12-18 MB board-meeting packet: the
    statement, the investor presentation and the press release in one file.
    Sent whole, the prompt overwhelms the model and the reply truncates or comes
    back empty -- that was the largest single failure class in the first batches.
    There is no smaller document to choose, so the fix is to send less of it.

    Scores each page on statement vocabulary and keeps the best contiguous run.
    Returns the original bytes unchanged when trimming is unnecessary or fails,
    since a whole document is better than a wrongly cropped one.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return pdf_bytes, "pypdf missing; sent whole"

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total = len(reader.pages)
        if total <= max_pages:
            return pdf_bytes, f"{total}p, sent whole"

        scores = []
        for page in reader.pages:
            try:
                text = (page.extract_text() or "").lower()
            except Exception:  # noqa: BLE001 - a bad page scores zero
                text = ""
            scores.append(sum(marker in text for marker in STATEMENT_MARKERS))

        if max(scores) == 0:
            return pdf_bytes, f"{total}p, no statement page found; sent whole"

        # Best window of consecutive pages, so a statement split across pages
        # stays intact.
        window = min(max_pages, total)
        best_start = max(
            range(total - window + 1), key=lambda i: sum(scores[i:i + window])
        )

        writer = PdfWriter()
        for page in reader.pages[best_start:best_start + window]:
            writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        trimmed = buffer.getvalue()

        if len(trimmed) >= len(pdf_bytes):
            return pdf_bytes, f"{total}p, trim gained nothing"
        return trimmed, (f"{total}p -> pages {best_start + 1}-{best_start + window}, "
                         f"{len(pdf_bytes)/1e6:.1f}MB -> {len(trimmed)/1e6:.1f}MB")
    except Exception as exc:  # noqa: BLE001 - never let trimming break extraction
        log.warning("pdf trim failed: %s", exc)
        return pdf_bytes, f"trim failed: {type(exc).__name__}"


_SIZE = re.compile(r"([\d.]+)\s*(KB|MB|GB)", re.I)
# Usable documents in the validation sample ran 0.2-7.2 MB. Below the floor is a
# cover letter with no table; above the ceiling is a board-meeting packet with
# the investor presentation bundled in, which truncates the model's reply.
SIZE_FLOOR_MB, SIZE_CEILING_MB = 0.1, 9.0


def _parse_file_size(value) -> float | None:
    """NSE reports attachment size as '809.36 KB' / '1.41 MB'."""
    if not isinstance(value, str):
        return None
    match = _SIZE.search(value)
    if not match:
        return None
    number, unit = float(match.group(1)), match.group(2).upper()
    return number * {"KB": 1e-3, "MB": 1.0, "GB": 1e3}[unit]


def _size_penalty(size_mb) -> int:
    """0 = comfortably usable, 1 = marginal, 2 = known to fail."""
    if size_mb is None:
        return 1
    if size_mb > SIZE_CEILING_MB or size_mb < SIZE_FLOOR_MB:
        return 2
    return 0


def _parse_announcement_date(value) -> dt.date | None:
    """NSE stamps announcements as '12-Aug-2026 16:31:19'."""
    if not isinstance(value, str):
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I | re.M)


def parse_json_payload(text: str) -> dict:
    """Recover the JSON object from a reply that may carry prose or fences.

    Models differ in how literally they take "JSON only" — some prepend a
    heading, some wrap in fences. Since the point here is to compare models on
    extraction accuracy, formatting habits should not decide the contest.
    """
    if not text:
        raise ProviderError("empty response")

    cleaned = _FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost braces, which survives prose on either side.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ProviderError(f"no JSON object in response: {cleaned[:160]!r}")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError(f"malformed JSON: {exc}; began {cleaned[start:start+160]!r}") from exc


class BSEPDFProvider:
    """Fetch BSE result bundles and extract financials from the PDFs inside."""

    name = "bse_pdf"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.LLM_MODEL
        self._session: cr.Session | None = None
        self._openai = None
        self._caps: dict | None = None

    # --------------------------------------------------------- capabilities
    def capabilities(self) -> dict:
        """What this model can actually accept, from OpenRouter's model list.

        Models differ in ways that decide how the request must be shaped, and
        guessing produces a bare 404 or a silently unenforced schema:

          * no `file` modality  -> the `native` PDF engine is unavailable, so
            the document has to be parsed upstream instead.
          * no `tools`          -> forced tool-calling is impossible and the
            weaker `response_format` path is the only option.

        Qwen2.5-VL-72B is exactly this case: image-only input, no tool support.
        """
        if self._caps is None:
            import urllib.request

            try:
                request = urllib.request.Request(
                    "https://openrouter.ai/api/v1/models",
                    headers={"User-Agent": "AI-Investo/0.1"},
                )
                with urllib.request.urlopen(request, timeout=45) as handle:
                    models = json.load(handle)["data"]
            except Exception as exc:  # noqa: BLE001 - fall back to assuming the best
                log.warning("could not read model capabilities: %s", exc)
                self._caps = {"tools": True, "file": True, "known": False}
                return self._caps

            entry = next((m for m in models if m["id"] == self.model), None)
            if entry is None:
                raise ProviderError(
                    f"{self.model} is not offered by OpenRouter. Check the id at "
                    "https://openrouter.ai/models"
                )
            modalities = (entry.get("architecture") or {}).get("input_modalities") or []
            supported = entry.get("supported_parameters") or []
            self._caps = {
                "tools": "tools" in supported,
                "structured_outputs": "structured_outputs" in supported,
                "file": "file" in modalities,
                "image": "image" in modalities,
                "known": True,
            }
        return self._caps

    def pdf_engine(self) -> str:
        """`native` only where the model really accepts files.

        Asking for `native` on an image-only model silently falls back to
        OpenRouter's default, which bills mistral-ocr at $2/1,000 pages without
        saying so. Choosing it explicitly at least makes the cost visible.
        """
        configured = settings.PDF_ENGINE
        if configured == "native" and not self.capabilities().get("file", True):
            log.info("%s has no file input; using mistral-ocr", self.model)
            return "mistral-ocr"
        return configured

    # ------------------------------------------------------------- transport
    def _bse(self) -> cr.Session:
        if self._session is None:
            session = cr.Session(impersonate=BROWSER, headers=HEADERS)
            session.get(f"{BSE_WWW}/", timeout=settings.REQUEST_TIMEOUT)
            self._session = session
        return self._session

    def _client(self):
        """OpenRouter client, created lazily with an actionable error if unset."""
        if self._openai is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover
                raise ProviderError("pip install openai") from exc

            key = os.getenv("OPENROUTER_API_KEY")
            if not key:
                raise ProviderError(
                    "OPENROUTER_API_KEY is not set. Add it to C:\\AI-Investo\\.env "
                    "(already gitignored) as OPENROUTER_API_KEY=sk-or-v1-..., "
                    "or set it in the environment. Get a key at openrouter.ai/keys."
                )
            self._openai = OpenAI(base_url=settings.OPENROUTER_BASE_URL, api_key=key)
        return self._openai

    # ---------------------------------------------------- document discovery
    def fetch_result_documents(self, symbol: str) -> pd.DataFrame:
        """Result-statement PDFs a company has filed, from NSE announcements.

        NOT from BSE's `FinancialResult` endpoint. That endpoint accepts a
        scripcode and silently ignores it: WABAG, Siemens, ABB and UltraTech all
        returned byte-identical bundles containing BSE Limited's OWN accounts.
        Every extraction built on it was reading the wrong company, and nothing
        in the response indicated a problem.

        NSE's corporate-announcements API is keyed by symbol, returns full
        attachment URLs, and carries the announcement timestamp -- which doubles
        as the filing_date the point-in-time contract needs.
        """
        # Retried with a fresh handshake: NSE drops connections under sustained
        # use, and an unattended batch must not die on one slow response.
        response = None
        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                session = cr.Session(impersonate=BROWSER, headers={
                    **HEADERS, "Referer": f"{NSE_WWW}/get-quotes/equity?symbol={symbol}",
                })
                session.get(NSE_WWW, timeout=settings.REQUEST_TIMEOUT)
                response = session.get(
                    f"{NSE_WWW}/api/corporate-announcements?index=equities&symbol={symbol}",
                    timeout=90,
                )
                if response.status_code == 200:
                    break
                last = ProviderError(f"HTTP {response.status_code}")
                response = None
            except Exception as exc:  # noqa: BLE001 - retried below
                last, response = exc, None
            time.sleep(attempt * 2.0)

        if response is None:
            raise ProviderError(f"NSE announcements failed for {symbol}: {last}")

        try:
            rows = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"announcements not JSON for {symbol}: {exc}") from exc

        out = []
        for record in rows or []:
            desc = record.get("desc") or ""
            text = f"{desc} {record.get('attchmntText') or ''}"
            if not RESULT_ANNOUNCEMENT.search(text):
                continue
            url = record.get("attchmntFile") or ""
            if not url.lower().endswith(".pdf"):
                continue
            period_end = parse_period_end(text)
            if period_end is None:
                continue
            announced = _parse_announcement_date(record.get("an_dt"))
            out.append({
                "symbol": symbol,
                "period_end": period_end,
                "filing_date": announced,
                "pdf_url": url,
                "desc": desc,
                "size_mb": _parse_file_size(record.get("attFileSize")),
                "text": text[:300],
            })

        frame = pd.DataFrame(out)
        if frame.empty:
            return frame

        # Prefer the results filing over later newspaper reprints, then prefer a
        # document in a workable SIZE BAND. Measured: extraction fails on the
        # 12-18 MB board-meeting packets that bundle the investor presentation
        # (the prompt overwhelms the model and the reply truncates), and equally
        # on sub-100 KB cover letters that contain no table at all. The usable
        # documents in the validation sample were 0.2-7.2 MB.
        frame["rank"] = frame["desc"].map(
            lambda d: 0 if re.search(r"outcome|integrated filing|financial result", d, re.I) else 1
        )
        frame["size_penalty"] = frame["size_mb"].map(_size_penalty)
        return (frame.sort_values(["period_end", "size_penalty", "rank", "filing_date"],
                                  ascending=[False, True, True, True])
                .drop_duplicates("period_end", keep="first")
                .drop(columns=["rank", "size_penalty"]).reset_index(drop=True))

    def fetch_pdf(self, url: str) -> bytes:
        """Download an announcement attachment."""
        session = cr.Session(impersonate=BROWSER, headers={
            **HEADERS, "Referer": f"{NSE_WWW}/companies-listing/corporate-filings-announcements",
        })
        session.get(NSE_WWW, timeout=settings.REQUEST_TIMEOUT)
        response = session.get(url, timeout=120)
        if response.status_code != 200:
            raise ProviderError(f"PDF HTTP {response.status_code} for {url[-60:]}")
        if not response.content.startswith(b"%PDF"):
            raise ProviderError(f"not a PDF: {url[-60:]}")
        return response.content

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
        filename: str = "results.pdf",
    ) -> tuple[QuarterlyFinancials, dict]:
        """Read one period's figures out of a results PDF.

        Returns (financials, usage) where usage carries OpenRouter's reported
        cost for the call, so a bulk run can report real spend rather than an
        estimate.
        """
        pdf_bytes, trim_note = trim_to_statement(pdf_bytes)
        log.info("pdf: %s", trim_note)

        if len(pdf_bytes) > 30 * 1024 * 1024:
            raise ProviderError(f"pdf too large for one request: {len(pdf_bytes):,} bytes")

        client = self._client()
        data_url = ("data:application/pdf;base64,"
                    + base64.standard_b64encode(pdf_bytes).decode("ascii"))

        instruction = (
            f"Extract the {basis.lower()} figures for the quarter that starts "
            f"{period_start:%d %B %Y} and ends {period_end:%d %B %Y} — a three-month "
            f"period.\n\n"
            f"Do NOT read the year-to-date column, which also ends {period_end:%d %B %Y} "
            f"but starts at the beginning of the financial year. Confirm the column you "
            f"used by returning its period in period_start and period_end."
        )

        # Forced tool-calling wherever the model supports it. Measured on
        # anthropic/claude-sonnet-5 via OpenRouter: response_format with
        # strict:true was accepted and then ignored -- replies invented a
        # `company` field, returned "Rs in Lakhs" where an enum was required, and
        # nested other_income as an object. Providers enforce tool input schemas
        # far more consistently, and forcing the call leaves no free-text path.
        schema = strict_schema(QuarterlyFinancials)
        if self.capabilities().get("tools", True):
            shape = {
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "description": "Record the extracted figures for one reporting period.",
                        "parameters": schema,
                    },
                }],
                "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
            }
        else:
            # Only for models without tool support, Qwen2.5-VL-72B among them.
            shape = {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "quarterly_financials",
                                    "strict": True, "schema": schema},
                },
            }

        try:
            # No `temperature`: Claude models on OpenRouter do not list it as a
            # supported parameter (reasoning models fix it), and with
            # require_parameters set, sending it disqualifies every endpoint and
            # returns a bare 404. The strict schema plus an explicit prompt does
            # the work determinism would have.
            # anthropic/claude-sonnet-5 via OpenRouter: response_format with
            # strict:true was accepted and then ignored -- replies invented a
            # `company` field, returned "Rs in Lakhs" where an enum was required,
            # and nested other_income as an object. Providers enforce tool input
            # schemas far more consistently than response_format, and forcing the
            # call leaves the model no free-text path.
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=8000,
                **shape,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "file",
                         "file": {"filename": filename, "file_data": data_url}},
                        {"type": "text", "text": instruction},
                    ]},
                ],
                extra_body={
                    # 'native' avoids OpenRouter's mistral-ocr default, which
                    # bills $2/1,000 pages to OCR documents that are already text.
                    "plugins": [{"id": "file-parser",
                                 "pdf": {"engine": self.pdf_engine()}}],
                    # Refuse to silently route to an endpoint that ignores the
                    # schema; an unvalidated blob is worse than a clear failure.
                    "provider": {"require_parameters": True},
                    "usage": {"include": True},
                },
                extra_headers={
                    "HTTP-Referer": "https://github.com/local/ai-investo",
                    "X-Title": "AI-Investo",
                },
            )
        except Exception as exc:  # noqa: BLE001 - surfaced with context
            # A spent balance is not an extraction failure. Left as a normal
            # error it would march through the universe marking every statement
            # FAILED at zero cost, and the real reason would be buried in
            # thousands of rows. Raise a distinct type so the batch stops.
            text = str(exc)
            if "402" in text or "requires at least" in text or "insufficient" in text.lower():
                raise InsufficientCredit(
                    "OpenRouter balance too low for file requests. Top up at "
                    "https://openrouter.ai/settings/credits"
                ) from exc
            raise ProviderError(f"OpenRouter call failed: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise ProviderError("OpenRouter returned no choices")
        if choice.finish_reason == "content_filter":
            raise ProviderError("provider filtered this document")

        calls = getattr(choice.message, "tool_calls", None) or []
        if calls:
            payload = parse_json_payload(calls[0].function.arguments or "")
        else:
            # Some providers answer in prose despite a forced tool choice; the
            # content may still hold the object.
            payload = parse_json_payload(choice.message.content or "")

        # Some providers nest the arguments under the tool name rather than
        # returning them at the top level.
        if set(payload) == {TOOL_NAME} and isinstance(payload[TOOL_NAME], dict):
            payload = payload[TOOL_NAME]

        try:
            parsed = QuarterlyFinancials.model_validate(payload)
        except ValidationError as exc:
            raise ProviderError(f"response did not match the schema: {exc}") from exc

        raw_usage = response.usage
        usage = {
            "model": response.model,
            "prompt_tokens": getattr(raw_usage, "prompt_tokens", None),
            "completion_tokens": getattr(raw_usage, "completion_tokens", None),
            # OpenRouter adds `cost` in USD when usage.include is set.
            "cost_usd": getattr(raw_usage, "cost", None),
        }
        return parsed, usage

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
        got_end = _flexible_date(extracted.period_end)
        got_start = _flexible_date(extracted.period_start)
        if got_end is None or got_start is None:
            raise ProviderError(
                f"{symbol}: unparseable period "
                f"{extracted.period_start!r}..{extracted.period_end!r}"
            )
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

        values = reconcile(extracted.model_dump())

        multiplier = UNIT_TO_RUPEES[extracted.amounts_in]
        rows = []
        for metric, value in values.items():
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
