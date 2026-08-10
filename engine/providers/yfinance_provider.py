"""Yahoo Finance provider -- prices and profiles for global and Indian equities.

Scope note: the smoke test showed Yahoo serves individual Indian *stock* prices
reliably but returns near-empty history for Indian *index* series (^CNXSC gave
one row). Indices and the symbol master therefore come from the NSE provider;
this module is for security-level price and profile data only.

Fundamentals are deliberately NOT taken from here. Yahoo exposes no filing date,
and substituting the period end would leak future information into backtests --
see `fetch_fundamentals` below.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd
import yfinance as yf
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from engine.config import settings
from engine.providers.base import (
    OHLCV_COLUMNS,
    PROFILE_COLUMNS,
    MarketDataProvider,
    empty,
)

log = logging.getLogger(__name__)

# Yahoo suffix -> (exchange, country, currency)
SUFFIX_MAP = {
    ".NS": ("NSE", "IN", "INR"),
    ".BO": ("BSE", "IN", "INR"),
    ".TW": ("TWSE", "TW", "TWD"),
    ".T": ("TSE", "JP", "JPY"),
    ".KS": ("KRX", "KR", "KRW"),
    ".HK": ("HKEX", "HK", "HKD"),
    ".DE": ("XETRA", "DE", "EUR"),
    ".L": ("LSE", "GB", "GBP"),
    ".PA": ("EPA", "FR", "EUR"),
    ".SW": ("SIX", "CH", "CHF"),
}


def classify_ticker(ticker: str) -> tuple[str, str, str]:
    """Infer (exchange, country, currency) from a Yahoo symbol suffix."""
    for suffix, meta in SUFFIX_MAP.items():
        if ticker.endswith(suffix):
            return meta
    return ("US", "US", "USD")


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"
    supports_fundamentals = False

    def __init__(self, batch_size: int | None = None) -> None:
        self.batch_size = batch_size or settings.BATCH_SIZE

    # ------------------------------------------------------------------ ohlcv
    def fetch_ohlcv(
        self,
        tickers: list[str],
        start: dt.date | str,
        end: dt.date | str | None = None,
    ) -> pd.DataFrame:
        if not tickers:
            return empty(OHLCV_COLUMNS)

        frames: list[pd.DataFrame] = []
        batches = [
            tickers[i : i + self.batch_size]
            for i in range(0, len(tickers), self.batch_size)
        ]
        for n, batch in enumerate(batches, 1):
            log.info("ohlcv batch %d/%d (%d tickers)", n, len(batches), len(batch))
            try:
                raw = self._download(batch, start, end)
            except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the run
                log.warning("batch %d failed: %s", n, exc)
                continue
            frames.extend(self._normalise(raw, batch))
            time.sleep(settings.RATE_LIMIT_SLEEP)

        if not frames:
            return empty(OHLCV_COLUMNS)

        out = pd.concat(frames, ignore_index=True)
        out = out.dropna(subset=["date", "adj_close"])
        return out.reindex(columns=OHLCV_COLUMNS)

    @retry(
        stop=stop_after_attempt(settings.MAX_RETRIES),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def _download(
        self, batch: list[str], start: dt.date | str, end: dt.date | str | None
    ) -> pd.DataFrame:
        # auto_adjust=False keeps a separate 'Adj Close' so raw and adjusted
        # closes stay distinguishable; some yfinance versions drop it anyway,
        # which _normalise handles.
        return yf.download(
            tickers=batch,
            start=str(start),
            end=str(end) if end else None,
            interval="1d",
            auto_adjust=False,
            actions=False,
            group_by="ticker",
            threads=True,
            progress=False,
        )

    @staticmethod
    def _normalise(raw: pd.DataFrame, batch: list[str]) -> list[pd.DataFrame]:
        """Flatten yfinance output into one long frame per ticker."""
        if raw is None or raw.empty:
            return []

        out: list[pd.DataFrame] = []
        multi = isinstance(raw.columns, pd.MultiIndex)

        for ticker in batch:
            try:
                sub = raw[ticker] if multi else raw
            except KeyError:
                continue
            if sub is None or sub.empty:
                continue

            sub = sub.reset_index()
            date_col = "Date" if "Date" in sub.columns else sub.columns[0]
            frame = pd.DataFrame({
                "ticker": ticker,
                "date": pd.to_datetime(sub[date_col]).dt.date,
                "open": sub.get("Open"),
                "high": sub.get("High"),
                "low": sub.get("Low"),
                "close": sub.get("Close"),
                "adj_close": sub.get("Adj Close", sub.get("Close")),
                "volume": sub.get("Volume"),
            })
            frame = frame.dropna(subset=["adj_close"])
            if not frame.empty:
                out.append(frame)
        return out

    # ---------------------------------------------------------------- profile
    def fetch_profile(self, tickers: list[str]) -> pd.DataFrame:
        """Per-ticker metadata. Slow (one request each), so call it sparingly."""
        rows = []
        for ticker in tickers:
            exchange, country, currency = classify_ticker(ticker)
            record = {
                "ticker": ticker, "name": None, "exchange": exchange,
                "country": country, "currency": currency, "sector": None,
                "industry": None, "market_cap": None, "isin": None,
            }
            try:
                info = yf.Ticker(ticker).get_info() or {}
                record.update({
                    "name": info.get("longName") or info.get("shortName"),
                    "sector": info.get("sector"),
                    "industry": info.get("industry"),
                    "market_cap": info.get("marketCap"),
                    "currency": info.get("currency") or currency,
                })
            except Exception as exc:  # noqa: BLE001 - metadata is best-effort
                log.debug("profile failed for %s: %s", ticker, exc)
            rows.append(record)
            time.sleep(settings.RATE_LIMIT_SLEEP)

        return pd.DataFrame(rows).reindex(columns=PROFILE_COLUMNS)

    # ----------------------------------------------------------- fundamentals
    def fetch_fundamentals(self, tickers: list[str]) -> pd.DataFrame:
        """Not supported, by choice.

        Yahoo returns statements keyed by period end with no filing date. Using
        the period end as a stand-in would make every figure appear public up to
        ~45 days before it actually was, inflating backtested returns. Reported
        financials come from NSE/BSE XBRL filings instead, which carry a real
        submission timestamp.
        """
        raise NotImplementedError(
            "yfinance exposes no filing date; use the XBRL provider for "
            "fundamentals to preserve point-in-time correctness."
        )
