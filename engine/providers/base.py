"""Provider interface.

Every data source implements this. The engine never imports a concrete provider
directly -- it asks the registry -- so swapping the free stack for a paid feed
(EODHD, FMP) is a config change rather than a rewrite.

Normalised contracts, so downstream code never learns a provider's quirks:

    fetch_ohlcv  -> ticker, date, open, high, low, close, adj_close, volume
    fetch_profile-> ticker, name, exchange, country, currency, sector,
                    industry, market_cap, isin
    fetch_fundamentals -> ticker, period_end, period_type, filing_date,
                          metric, value, unit
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod

import pandas as pd

OHLCV_COLUMNS = [
    "ticker", "date", "open", "high", "low", "close", "adj_close", "volume",
]
PROFILE_COLUMNS = [
    "ticker", "name", "exchange", "country", "currency", "sector", "industry",
    "market_cap", "isin",
]
FUNDAMENTAL_COLUMNS = [
    "ticker", "period_end", "period_type", "filing_date", "metric", "value", "unit",
]


class ProviderError(RuntimeError):
    """Raised when a provider fails in a way the caller should notice."""


class MarketDataProvider(ABC):
    """A source of market data."""

    name: str = "base"
    supports_fundamentals: bool = False

    @abstractmethod
    def fetch_ohlcv(
        self,
        tickers: list[str],
        start: dt.date | str,
        end: dt.date | str | None = None,
    ) -> pd.DataFrame:
        """Daily bars for `tickers`. Returns an empty frame if nothing resolves."""

    def fetch_profile(self, tickers: list[str]) -> pd.DataFrame:
        """Descriptive metadata. Providers without profiles return empty."""
        return pd.DataFrame(columns=PROFILE_COLUMNS)

    def fetch_fundamentals(self, tickers: list[str]) -> pd.DataFrame:
        """Reported financials in long form.

        `filing_date` must be the date the figure became public -- never the
        period end. A provider that cannot supply a true filing date should
        say so rather than substituting the period end, which would silently
        corrupt every backtest.
        """
        return pd.DataFrame(columns=FUNDAMENTAL_COLUMNS)


def empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
