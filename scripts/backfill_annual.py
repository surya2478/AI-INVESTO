"""Backfill annual (and quarterly) point-in-time fundamentals from NSE XBRL.

Separate from the nightly job because it is a one-off of a different shape: a
few thousand HTTP fetches that only needs doing once per company, against a
source that rate-limits and occasionally drops a document.

COVERAGE CEILING, measured rather than assumed:
  * annual XBRL documents exist from roughly FY2018,
  * they carry the P&L throughout,
  * but the balance sheet and cash-flow statement only from FY2023.
So this restores the annual history the score ranks on, and does NOT restore
enough balance-sheet history to test the quality pillar. See docs/BACKTEST.md.

    python scripts/backfill_annual.py --limit 20
    python scripts/backfill_annual.py --all
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# `python scripts/backfill_annual.py` puts scripts/ on the path, not the repo
# root, so `engine` is not importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.fundamentals import sync_fundamentals  # noqa: E402
from engine.storage import db  # noqa: E402
from engine.universe.builder import investable_universe  # noqa: E402
from engine.universe.theme_graph import load_theme_graph  # noqa: E402

log = logging.getLogger("backfill")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="cap symbols (0 = no cap)")
    parser.add_argument("--all", action="store_true",
                        help="whole investable universe rather than theme names only")
    parser.add_argument("--max-annual", type=int, default=10)
    parser.add_argument("--max-filings", type=int, default=12,
                        help="quarterly filings per company (0 to skip)")
    parser.add_argument("--chunk", type=int, default=25,
                        help="commit and report every N companies")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    con = db.connect()
    try:
        tickers = investable_universe(con) if args.all else load_theme_graph().india_universe()
        symbols = [t.removesuffix(".NS") for t in tickers if t.endswith(".NS")]
        if args.limit:
            symbols = symbols[: args.limit]

        log.info("backfilling %d companies (%d annual + %d quarterly each)",
                 len(symbols), args.max_annual, args.max_filings)
        started = time.time()
        totals = {"written": 0, "ok": 0, "empty": [], "failed": []}

        for start in range(0, len(symbols), args.chunk):
            batch = symbols[start:start + args.chunk]
            result = sync_fundamentals(con, batch, max_filings=args.max_filings,
                                       max_annual=args.max_annual)
            totals["written"] += result["written"]
            totals["ok"] += len(result["ok"])
            totals["empty"] += result["empty"]
            totals["failed"] += result["failed"]
            done = min(start + args.chunk, len(symbols))
            log.info("%d/%d companies | %d facts | %.1f min elapsed",
                     done, len(symbols), totals["written"], (time.time() - started) / 60)

        log.info("DONE: %d facts from %d companies, %d empty, %d failed",
                 totals["written"], totals["ok"], len(totals["empty"]), len(totals["failed"]))
        for symbol, reason in totals["failed"][:20]:
            log.warning("failed %s: %s", symbol, reason)
        if totals["empty"]:
            log.warning("no data returned for: %s", ", ".join(totals["empty"][:30]))

        coverage = con.execute("""
            SELECT period_end, count(DISTINCT security_id) AS companies, count(*) AS facts
              FROM fundamentals_pit
             WHERE source = 'nse_xbrl' AND period_type = 'A'
             GROUP BY 1 ORDER BY 1 DESC
        """).df()
        log.info("annual PIT coverage:\n%s", coverage.to_string(index=False))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
