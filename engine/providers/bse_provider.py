"""BSE provider -- scrip master, current ratios, and result filing dates.

Three things NSE does not give us:

1. `fetch_scrip_master` -- ISIN to BSE scrip code, plus market cap. The scrip
   code is needed to reach BSE's result bundles, and market cap feeds the
   Discovery pillar. Joined on ISIN, never on a guessed code.
2. `fetch_ratios` -- current EPS/PE/PB/ROE/OPM/NPM per company, structured and
   free, covering part of the Quality and Valuation pillars with no extraction.
3. `fetch_result_announcements` -- dissemination timestamps for results, which
   is the filing_date the point-in-time contract runs on.

On (3): querying without a scrip code returns every company's announcements for
a date window, ~1,900 rows across a results season. Backfilling the whole market
costs roughly 150 requests a year rather than one per company per quarter.

CAUTION on BSE's industry labels -- they misclassify (scrip 543264 comes back as
"Healthcare" for an electronics manufacturer). NSE's constituent-file industry
is used instead; only the numeric fields here are trusted.
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

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BROWSER = "chrome"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
QUARTER_ENDS = {(3, 31), (6, 30), (9, 30), (12, 31)}

# "...For The Quarter Ended June 30, 2026" / "Quarter Ended 30.06.2026" /
# "Quarter And Nine Months Period Ended December 31, 2025"
_ENDED = re.compile(
    r"ended\s+(?:on\s+)?"
    r"(?:(\d{1,2})[.\-/\s]+([A-Za-z]{3,9}|\d{1,2})[.,\-/\s]+(\d{4})"
    r"|([A-Za-z]{3,9})\s+(\d{1,2})?,?\s*(\d{4}))",
    re.I,
)


def parse_period_end(subject: str) -> dt.date | None:
    """Pull the reporting period end out of an announcement subject.

    Returns None rather than guessing when the text does not clearly name a
    quarter end -- a wrong period end would attach a filing date to the wrong
    quarter, which is worse than having no date at all.
    """
    if not subject:
        return None
    match = _ENDED.search(subject.replace("\r", " ").replace("\n", " "))
    if not match:
        return None

    day = month = year = None
    if match.group(3):                       # 30 June 2026 / 30.06.2026
        day, raw_month, year = match.group(1), match.group(2), match.group(3)
        month = MONTHS.get(raw_month[:3].lower()) if raw_month.isalpha() else int(raw_month)
        day, year = int(day), int(year)
    elif match.group(6):                     # June 30, 2026 / June 2026
        month = MONTHS.get(match.group(4)[:3].lower())
        year = int(match.group(6))
        day = int(match.group(5)) if match.group(5) else None

    if not month or not year or not 1 <= month <= 12:
        return None

    if day is None:                          # month named without a day
        day = 30 if month in (6, 9) else 31
    try:
        candidate = dt.date(year, month, day)
    except ValueError:
        return None

    # Only accept genuine quarter ends; anything else is a different disclosure.
    if (candidate.month, candidate.day) in QUARTER_ENDS:
        return candidate
    if candidate.month in (3, 6, 9, 12) and candidate.day >= 28:
        return dt.date(year, month, 30 if month in (6, 9) else 31)
    return None


class BSEProvider:
    name = "bse"

    def __init__(self) -> None:
        self._session: cr.Session | None = None

    def _warm(self) -> cr.Session:
        session = cr.Session(impersonate=BROWSER, headers=HEADERS)
        session.get("https://www.bseindia.com/", timeout=settings.REQUEST_TIMEOUT)
        return session

    def _get(self, url: str, timeout: float | None = None, tries: int = 4):
        """GET with backoff and session re-warm.

        BSE throttles hard under sustained pagination -- it stops responding
        entirely rather than returning 429, surfacing as a curl timeout with
        zero bytes. A dropped session is recoverable, so rebuild it and back off
        rather than failing the run.
        """
        last: Exception | None = None
        for attempt in range(1, tries + 1):
            try:
                if self._session is None:
                    self._session = self._warm()
                response = self._session.get(
                    url, timeout=timeout or settings.REQUEST_TIMEOUT
                )
                if response.status_code == 200:
                    return response
                last = ProviderError(f"BSE HTTP {response.status_code} for {url[:90]}")
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                last = exc
                self._session = None          # force a fresh handshake
            if attempt < tries:
                time.sleep(min(2.0 * attempt, 12.0))
        raise ProviderError(f"BSE request failed after {tries} tries: {last}")

    # ------------------------------------------------------------ scrip master
    def fetch_scrip_master(self) -> pd.DataFrame:
        """Active equity scrips with ISIN, scrip code and market cap."""
        response = self._get(
            f"{BSE_API}/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active",
            timeout=90,
        )
        rows = response.json()
        if not rows:
            return pd.DataFrame()

        frame = pd.DataFrame(rows)
        # Mktcap arrives in Rs CRORES (Reliance 1,781,491 = Rs 17.8 lakh crore),
        # but `securities.market_cap` is denominated in the security's currency,
        # i.e. rupees. Convert here so every consumer sees one unit -- leaving it
        # in crores made the market-cap gate reject the entire universe.
        return pd.DataFrame({
            "isin": frame["ISIN_NUMBER"].astype("string").str.strip(),
            "scripcode": frame["SCRIP_CD"].astype("string").str.strip(),
            "bse_name": frame.get("Scrip_Name"),
            "market_cap": pd.to_numeric(frame.get("Mktcap"), errors="coerce") * 1e7,
            "face_value": pd.to_numeric(frame.get("FACE_VALUE"), errors="coerce"),
        }).dropna(subset=["isin", "scripcode"])

    # ----------------------------------------------------------------- ratios
    def fetch_ratios(self, scripcode: str) -> dict:
        """Headline ratios for one company. Numeric fields only -- see module note."""
        response = self._get(f"{BSE_API}/ComHeadernew/w?quotetype=EQ&scripcode={scripcode}&seriesid=")
        payload = response.json() or {}

        def number(key: str) -> float | None:
            try:
                value = payload.get(key)
                return float(value) if value not in (None, "", "-") else None
            except (TypeError, ValueError):
                return None

        return {
            "scripcode": scripcode,
            "eps": number("EPS"), "ceps": number("CEPS"), "pe": number("PE"),
            "pb": number("PB"), "roe": number("ROE"),
            "opm": number("OPM"), "npm": number("NPM"),
        }

    # ------------------------------------------------------------ filing dates
    def fetch_result_announcements(
        self, from_date: dt.date, to_date: dt.date, max_pages: int = 60
    ) -> pd.DataFrame:
        """Every company's result announcements in a date window.

        Windows longer than about three weeks are rejected by BSE, and a future
        `to_date` returns an error row, so callers should chunk and stay in the
        past. Returns the dissemination timestamp, which is the filing date.
        """
        if to_date >= dt.date.today():
            to_date = dt.date.today() - dt.timedelta(days=1)
        if from_date > to_date:
            return pd.DataFrame()

        collected: list[dict] = []
        for page in range(1, max_pages + 1):
            url = (f"{BSE_API}/AnnSubCategoryGetData/w?pageno={page}&strCat=Result"
                   f"&strPrevDate={from_date:%Y%m%d}&strScrip=&strSearch=P"
                   f"&strToDate={to_date:%Y%m%d}&strType=C&subcategory=")
            try:
                payload = self._get(url, timeout=60).json()
            except (ProviderError, ValueError) as exc:
                # Keep what this window already yielded; a later window may work.
                log.warning("page %d of %s..%s failed, stopping window: %s",
                            page, from_date, to_date, exc)
                break
            rows = payload.get("Table", []) if isinstance(payload, dict) else payload
            if not rows:
                break
            # A single "Future Date cannot be selected" row means a bad window.
            if len(rows) == 1 and "Column1" in rows[0]:
                log.warning("BSE rejected window %s..%s: %s", from_date, to_date,
                            rows[0].get("Column1"))
                break
            collected.extend(rows)

            total = None
            if isinstance(payload, dict) and payload.get("Table1"):
                total = (payload["Table1"][0] or {}).get("ROWCNT")
            if total and len(collected) >= int(total):
                break
            # BSE throttles harder than NSE; pace pagination well below the
            # shared default rather than discovering the limit by being cut off.
            time.sleep(max(settings.RATE_LIMIT_SLEEP, 1.2))

        if not collected:
            return pd.DataFrame()

        frame = pd.DataFrame(collected)
        subject = frame.get("NEWSSUB", pd.Series(dtype="object")).fillna(
            frame.get("HEADLINE", pd.Series(dtype="object"))
        )
        out = pd.DataFrame({
            "scripcode": frame["SCRIP_CD"].astype("string").str.strip(),
            "filing_ts": pd.to_datetime(frame["NEWS_DT"], errors="coerce"),
            "subject": subject,
        })
        out["filing_date"] = out["filing_ts"].dt.date
        out["period_end"] = out["subject"].map(parse_period_end)
        return out.dropna(subset=["scripcode", "filing_date"]).reset_index(drop=True)

    def fetch_result_announcements_range(
        self, start: dt.date, end: dt.date, window_days: int = 18
    ) -> pd.DataFrame:
        """Walk a long span in windows BSE will accept.

        Resilient by window: a throttled or failed window is logged and skipped
        so the rest of the range still lands. Losing the whole backfill to one
        timeout is how the first run failed.
        """
        frames, cursor, failures = [], start, 0
        while cursor <= end:
            chunk_end = min(cursor + dt.timedelta(days=window_days), end)
            try:
                frames.append(self.fetch_result_announcements(cursor, chunk_end))
            except Exception as exc:  # noqa: BLE001 - one window must not end the run
                failures += 1
                log.warning("announcements %s..%s failed: %s", cursor, chunk_end, exc)
                time.sleep(5.0)          # let the throttle decay before continuing
            cursor = chunk_end + dt.timedelta(days=1)

        if failures:
            log.warning("%d window(s) failed; backfill is incomplete", failures)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["scripcode", "period_end", "filing_date"]
        )
