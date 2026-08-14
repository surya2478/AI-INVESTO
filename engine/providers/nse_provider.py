"""NSE provider -- symbol master, index constituents, surveillance and flows.

NSE fingerprints TLS and rejects plain `requests`, so every call goes through
curl_cffi impersonating a real Chrome handshake. Two surfaces with different
reliability:

  * nsearchives.nseindia.com CSVs -- static files, no cookie, dependable.
    Everything structural (symbol master, index constituents) comes from here.
  * www.nseindia.com/api/* JSON -- needs a cookie from a homepage visit first,
    and endpoints move without notice. Used only for ASM and FII/DII.

MEASURED, Aug 2026: `api/historical/indicesHistory` returns a block page rather
than data, so historical Indian index levels are NOT retrievable free. See
`fetch_all_indices` for how the engine works around that.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import time

import pandas as pd
from curl_cffi import requests as cr

from engine.config import settings
from engine.providers.base import ProviderError

log = logging.getLogger(__name__)

ARCHIVE = "https://nsearchives.nseindia.com/content"
WWW = "https://www.nseindia.com"
BROWSER = "chrome"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/csv,application/json,text/html,*/*",
}

# Index name -> archive filename. The microcap file uses an underscore the
# others do not; NSE is inconsistent here and the 404 is silent, so these are
# verified rather than inferred.
INDEX_FILES = {
    "NIFTY 50":              "ind_nifty50list.csv",
    "NIFTY 500":             "ind_nifty500list.csv",
    "NIFTY MIDSMALLCAP 400": "ind_niftymidsmallcap400list.csv",
    "NIFTY SMALLCAP 250":    "ind_niftysmallcap250list.csv",
    "NIFTY MICROCAP 250":    "ind_niftymicrocap250_list.csv",
    "NIFTY TOTAL MARKET":    "ind_niftytotalmarket_list.csv",
}


def _parse_date(value) -> dt.date | None:
    """Parse the date forms NSE uses: '30-Jun-2026' and '14-Aug-2026 16:31:52'."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
                "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class NSEProvider:
    """Structural and regulatory data for the Indian market."""

    name = "nse"

    def __init__(self) -> None:
        self._session: cr.Session | None = None

    # ------------------------------------------------------------- transport
    def _get(self, url: str, referer: str | None = None, tries: int = 3):
        """GET with browser impersonation and linear backoff."""
        last: Exception | None = None
        for attempt in range(1, tries + 1):
            try:
                headers = dict(HEADERS)
                if referer:
                    headers["Referer"] = referer
                response = cr.get(
                    url, headers=headers, impersonate=BROWSER,
                    timeout=settings.REQUEST_TIMEOUT,
                )
                if response.status_code == 200:
                    return response
                last = ProviderError(f"HTTP {response.status_code} for {url}")
            except Exception as exc:  # noqa: BLE001 - retried below
                last = exc
            time.sleep(attempt * 1.5)
        raise ProviderError(f"NSE request failed after {tries} tries: {last}")

    def _api_session(self) -> cr.Session:
        """Cookie-warmed session for www.nseindia.com/api/*."""
        if self._session is None:
            session = cr.Session(impersonate=BROWSER, headers=HEADERS)
            session.get(WWW, timeout=settings.REQUEST_TIMEOUT)
            time.sleep(0.6)
            self._session = session
        return self._session

    @staticmethod
    def _csv(response) -> pd.DataFrame:
        frame = pd.read_csv(io.StringIO(response.text))
        frame.columns = [c.strip().lower().replace(" ", "_") for c in frame.columns]
        return frame

    # --------------------------------------------------------- symbol master
    def fetch_symbol_master(self) -> pd.DataFrame:
        """Every NSE-listed equity, with ISIN and listing date.

        Only currently-listed names appear -- NSE publishes no delisted archive,
        which is the root of the survivorship caveat documented in the README.
        """
        raw = self._csv(self._get(f"{ARCHIVE}/equities/EQUITY_L.csv"))

        frame = pd.DataFrame({
            "exchange_symbol": raw["symbol"].str.strip(),
            "name": raw["name_of_company"].str.strip(),
            "series": raw["series"].str.strip(),
            "listing_date": pd.to_datetime(
                raw["date_of_listing"], format="%d-%b-%Y", errors="coerce"
            ).dt.date,
            "isin": raw["isin_number"].str.strip(),
            "face_value": pd.to_numeric(raw.get("face_value"), errors="coerce"),
        })

        # EQ is ordinary equity; BE/BZ are surveillance-restricted settlement
        # series we do not want in a long-horizon universe.
        frame = frame[frame["series"] == "EQ"].copy()
        frame["ticker"] = frame["exchange_symbol"] + ".NS"
        frame["exchange"] = "NSE"
        frame["country"] = "IN"
        frame["currency"] = "INR"
        frame["source"] = self.name
        return frame.reset_index(drop=True)

    # ----------------------------------------------------- index constituents
    def fetch_index_constituents(self, index_name: str) -> pd.DataFrame:
        """Current members of an NSE index, with the industry label."""
        filename = INDEX_FILES.get(index_name)
        if not filename:
            raise ProviderError(f"no archive file known for index {index_name!r}")

        raw = self._csv(self._get(f"{ARCHIVE}/indices/{filename}"))
        frame = pd.DataFrame({
            "exchange_symbol": raw["symbol"].str.strip(),
            "name": raw["company_name"].str.strip(),
            "industry": raw.get("industry", pd.Series(dtype="object")),
            "isin": raw["isin_code"].str.strip(),
        })
        frame["ticker"] = frame["exchange_symbol"] + ".NS"
        frame["index_name"] = index_name
        return frame.reset_index(drop=True)

    def fetch_all_index_constituents(self) -> pd.DataFrame:
        frames = []
        for index_name in INDEX_FILES:
            try:
                frames.append(self.fetch_index_constituents(index_name))
                log.info("fetched constituents for %s", index_name)
            except ProviderError as exc:
                log.warning("constituents failed for %s: %s", index_name, exc)
            time.sleep(settings.RATE_LIMIT_SLEEP)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # ------------------------------------------------------------- indices
    def fetch_all_indices(self) -> pd.DataFrame:
        """Today's level for every NSE index.

        Because `indicesHistory` is blocked, this is a *snapshot* source: run it
        nightly and history accumulates from the first run forward. It cannot
        backfill. Benchmarks needing long history are instead reconstructed from
        constituent prices, which we do have back to 2012.
        """
        session = self._api_session()
        response = session.get(
            f"{WWW}/api/allIndices",
            timeout=settings.REQUEST_TIMEOUT,
            headers={"Referer": f"{WWW}/market-data/live-market-indices"},
        )
        if response.status_code != 200:
            raise ProviderError(f"allIndices HTTP {response.status_code}")

        rows = response.json().get("data", [])
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame

        out = pd.DataFrame({
            "index_name": frame["index"],
            "date": dt.date.today(),
            "last": pd.to_numeric(frame["last"], errors="coerce"),
            "open": pd.to_numeric(frame.get("open"), errors="coerce"),
            "high": pd.to_numeric(frame.get("high"), errors="coerce"),
            "low": pd.to_numeric(frame.get("low"), errors="coerce"),
            "previous_close": pd.to_numeric(frame.get("previousClose"), errors="coerce"),
            "pct_change": pd.to_numeric(frame.get("percentChange"), errors="coerce"),
        })
        out["source"] = self.name
        return out

    # -------------------------------------------------------- surveillance
    def fetch_asm_list(self) -> pd.DataFrame:
        """Additional Surveillance Measure names -- a hard quality-gate reject.

        An ASM listing means NSE has flagged unusual price or volume behaviour.
        Whatever the cause, it is not where a long-horizon position belongs.
        """
        session = self._api_session()
        response = session.get(
            f"{WWW}/api/reportASM",
            timeout=settings.REQUEST_TIMEOUT,
            headers={"Referer": f"{WWW}/reports/asm"},
        )
        if response.status_code != 200:
            raise ProviderError(f"reportASM HTTP {response.status_code}")

        payload = response.json()
        rows = []
        for bucket in ("longterm", "shortterm"):
            for record in (payload.get(bucket) or {}).get("data", []) or []:
                rows.append({
                    "exchange_symbol": (record.get("symbol") or "").strip(),
                    "name": record.get("companyName"),
                    "isin": record.get("isin"),
                    "stage": record.get("asmSurvIndicator"),
                    "bucket": bucket,
                    "flagged_on": record.get("asmTime"),
                })

        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        frame = frame[frame["exchange_symbol"] != ""].copy()
        frame["ticker"] = frame["exchange_symbol"] + ".NS"
        frame["event_date"] = pd.to_datetime(
            frame["flagged_on"], format="%d-%b-%Y", errors="coerce"
        ).dt.date
        frame["source"] = self.name
        return frame.reset_index(drop=True)

    def fetch_promoter_pledge(self, symbol: str) -> dict | None:
        """Promoter holding and encumbrance for one company.

        CAREFUL WITH THE DENOMINATOR. NSE's `percSharesPledged` is pledged shares
        as a share of TOTAL ISSUED EQUITY, while the figure quoted in Indian
        markets -- and the one a 20% gate threshold assumes -- is pledged shares
        as a share of PROMOTER HOLDING. For WABAG the two are 9.12% and 47.8%.
        Taking the reported field at face value would understate pledge about
        fivefold and let genuinely encumbered promoters through the gate.

        Both are returned, with the promoter-relative measure named explicitly.
        """
        session = self._api_session()
        try:
            session.get(f"{WWW}/get-quotes/equity?symbol={symbol}",
                        timeout=settings.REQUEST_TIMEOUT)
            response = session.get(
                f"{WWW}/api/corporate-pledgedata?index=equities&symbol={symbol}",
                timeout=settings.REQUEST_TIMEOUT,
                headers={"Referer": f"{WWW}/get-quotes/equity?symbol={symbol}"},
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"pledge fetch failed for {symbol}: {exc}") from exc

        if response.status_code != 200:
            raise ProviderError(f"pledge HTTP {response.status_code} for {symbol}")

        try:
            rows = (response.json() or {}).get("data") or []
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"pledge payload for {symbol}: {exc}") from exc
        if not rows:
            return None

        record = rows[0]

        def number(key):
            raw = record.get(key)
            if raw in (None, "", "-"):
                return None
            try:
                return float(str(raw).replace(",", "").strip())
            except ValueError:
                return None

        pledged = number("numSharesPledged")
        promoter_shares = number("totPromoterHolding")
        pledge_of_promoter = (
            (pledged / promoter_shares * 100.0)
            if pledged is not None and promoter_shares else None
        )

        return {
            "symbol": symbol,
            "quarter_end": _parse_date(record.get("shp")),
            "filing_date": _parse_date(record.get("broadcastDt")),
            "promoter_pct": number("percPromoterHolding"),
            # The gate's measure: pledged as a share of what promoters hold.
            "promoter_pledge_pct": pledge_of_promoter,
            # As NSE reports it: pledged as a share of total issued equity.
            "pledged_of_equity_pct": number("percSharesPledged"),
            "shares_pledged": pledged,
            "promoter_shares": promoter_shares,
            "public_pct": number("totPublicHolding"),
        }

    def fetch_fii_dii(self) -> pd.DataFrame:
        """Latest daily FII and DII cash-market flows (Rs crore)."""
        session = self._api_session()
        response = session.get(
            f"{WWW}/api/fiidiiTradeReact",
            timeout=settings.REQUEST_TIMEOUT,
            headers={"Referer": f"{WWW}/reports/fii-dii"},
        )
        if response.status_code != 200:
            raise ProviderError(f"fiidiiTradeReact HTTP {response.status_code}")

        frame = pd.DataFrame(response.json())
        if frame.empty:
            return frame

        return pd.DataFrame({
            "date": pd.to_datetime(frame["date"], format="%d-%b-%Y", errors="coerce").dt.date,
            "category": frame["category"],
            "buy_value": pd.to_numeric(frame["buyValue"], errors="coerce"),
            "sell_value": pd.to_numeric(frame["sellValue"], errors="coerce"),
            "net_value": pd.to_numeric(frame["netValue"], errors="coerce"),
            "source": self.name,
        })
