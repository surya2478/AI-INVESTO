"""Smoke test: can we actually pull the data the engine depends on?

Run before trusting any downstream stage. Checks global equities, Indian
equities (NSE ``.NS`` suffix), indices, and commodity/FX proxies.
"""

import sys

import duckdb
import pandas as pd
import yfinance as yf

PROBES = {
    "global equity": ["NVDA", "TSM"],
    "india equity": ["RELIANCE.NS", "WABAG.NS", "IONEXCHANG.NS"],
    "india microcap": ["ENVIRO.NS", "ONWARDTEC.NS"],
    "index": ["^NSEI", "^CNXSC", "^GSPC"],
    "macro": ["DX-Y.NYB", "CL=F", "HG=F", "USDINR=X"],
}


def probe(tickers: list[str]) -> list[tuple[str, str]]:
    """Return (ticker, status) for a 6-month daily pull of each ticker."""
    out = []
    for t in tickers:
        try:
            df = yf.Ticker(t).history(period="6mo", interval="1d", auto_adjust=True)
            if df.empty:
                out.append((t, "EMPTY"))
            else:
                last = df.index[-1].date()
                out.append((t, f"{len(df):>4} rows, last {last}, close {df['Close'].iloc[-1]:,.2f}"))
        except Exception as exc:  # noqa: BLE001 - smoke test reports, never raises
            out.append((t, f"ERROR {type(exc).__name__}: {exc}"))
    return out


def main() -> int:
    print(f"pandas {pd.__version__} | duckdb {duckdb.__version__} | yfinance {yf.__version__}\n")

    failures = 0
    for group, tickers in PROBES.items():
        print(f"[{group}]")
        for ticker, status in probe(tickers):
            flag = " " if "rows" in status else "!"
            if flag == "!":
                failures += 1
            print(f"  {flag} {ticker:<14} {status}")
        print()

    # DuckDB round-trip: confirms the analytics engine can persist a frame.
    con = duckdb.connect(":memory:")
    frame = pd.DataFrame({"ticker": ["A", "B"], "close": [1.5, 2.5]})
    got = con.execute("SELECT count(*) AS n, sum(close) AS s FROM frame").fetchone()
    print(f"[duckdb] round-trip rows={got[0]} sum={got[1]}")

    print(f"\n{'PASS' if failures == 0 else f'{failures} probe(s) failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
