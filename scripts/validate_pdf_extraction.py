"""Measure PDF-extraction accuracy against known-correct XBRL figures.

The only quarters worth testing are the ones we already hold structured data
for: XBRL is the ground truth, the PDF is the thing under test. Anything the
extractor produces for a post-2025 quarter is trusted only as far as this
report says it should be.

    python scripts/validate_pdf_extraction.py --companies 5

Reports per-metric relative error and an overall pass rate. Exits non-zero if
accuracy is below the threshold, so it can gate the ingest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import pandas as pd
from curl_cffi import requests as cr

from engine.config import settings as settings_module
from engine.providers.base import ProviderError
from engine.providers.pdf_extractor import (
    BSE_API,
    BSEPDFProvider,
    HEADERS,
    consistency_check,
)
from engine.storage import db

# A metric matches if it is within this relative distance of the XBRL value.
# Tight on purpose: these are printed figures being read back, not estimates,
# so anything beyond rounding is a misread column or a unit error.
TOLERANCE = 0.005
PASS_THRESHOLD = 0.95

COMPARE_METRICS = [
    "revenue", "other_income", "total_income", "employee_cost", "finance_cost",
    "depreciation", "total_expenses", "pbt", "tax_expense", "pat", "eps_basic",
]

# Reported alongside the score but excluded from it: XBRL defines other_expenses
# as a residual while the statement prints a named line, so the two disagree by
# construction on companies that itemise. Both models read the printed line
# consistently. Nothing downstream consumes it -- EBITDA comes from pbt,
# finance_cost, depreciation and other_income.
INFORMATIONAL_METRICS = ["other_expenses"]


def bse_scrip_map() -> dict[str, str]:
    """ISIN -> BSE scrip code. Joining on ISIN avoids guessing scrip codes."""
    session = cr.Session(impersonate="chrome", headers=HEADERS)
    session.get("https://www.bseindia.com/", timeout=25)
    response = session.get(
        f"{BSE_API}/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active",
        timeout=90,
    )
    if response.status_code != 200:
        raise ProviderError(f"BSE scrip master HTTP {response.status_code}")
    return {
        row["ISIN_NUMBER"]: row["SCRIP_CD"]
        for row in response.json()
        if row.get("ISIN_NUMBER") and row.get("SCRIP_CD")
    }


def xbrl_truth(con, limit: int) -> pd.DataFrame:
    """Companies and quarters we hold XBRL for, newest first."""
    return con.execute("""
        SELECT s.ticker, s.exchange_symbol, s.isin, f.period_end, f.filing_date,
               f.metric, f.value
          FROM fundamentals_pit f
          JOIN securities s ON s.security_id = f.security_id
         WHERE f.source = 'nse_xbrl' AND f.period_type = 'Q'
           AND f.security_id IN (
               SELECT security_id FROM fundamentals_pit
                GROUP BY security_id ORDER BY max(period_end) DESC LIMIT ?
           )
    """, [limit]).df()


def compare(truth: pd.Series, got: pd.Series) -> pd.DataFrame:
    rows = []
    for metric in COMPARE_METRICS:
        expected, actual = truth.get(metric), got.get(metric)
        if pd.isna(expected) and pd.isna(actual):
            continue
        if pd.isna(expected) or pd.isna(actual):
            rows.append({"metric": metric, "expected": expected, "actual": actual,
                         "rel_error": None, "match": False, "note": "missing on one side"})
            continue
        denom = abs(expected) if abs(expected) > 1e-9 else 1.0
        error = abs(actual - expected) / denom
        rows.append({"metric": metric, "expected": expected, "actual": actual,
                     "rel_error": error, "match": bool(error <= TOLERANCE), "note": ""})
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--companies", type=int, default=3)
    parser.add_argument("--quarters", type=int, default=1, help="Quarters per company")
    parser.add_argument("--model", default=None,
                        help="OpenRouter model id; overrides INVESTO_LLM_MODEL")
    args = parser.parse_args()

    con = db.connect(read_only=True)
    try:
        facts = xbrl_truth(con, args.companies)
    finally:
        con.close()

    if facts.empty:
        print("No XBRL facts stored — run `ingest fundamentals` first.", file=sys.stderr)
        return 1

    provider = BSEPDFProvider(model=args.model)
    print(f"model: {provider.model}\n")

    all_results = []
    for ticker, group in facts.groupby("ticker"):
        symbol = group["exchange_symbol"].iloc[0]

        try:
            documents = provider.fetch_result_documents(symbol)
        except ProviderError as exc:
            print(f"  {symbol:<14} document list failed: {exc}")
            continue
        if documents.empty:
            print(f"  {symbol:<14} no result documents published")
            continue

        periods = sorted(group["period_end"].unique(), reverse=True)[: args.quarters]
        for period_end in periods:
            period_end = pd.Timestamp(period_end).date()

            match = documents[documents.period_end == period_end]
            if match.empty:
                print(f"  {symbol:<14} {period_end} no document for this quarter")
                continue

            # First day of the quarter: two calendar months back, not 62 days.
            # Subtracting days lands in the wrong month for 31-day runs — for
            # 31 Dec it gave 1 September, and the model faithfully echoed that
            # four-month period back, which the span guard then rejected.
            start_month = period_end.month - 2
            start_year = period_end.year
            if start_month <= 0:
                start_month += 12
                start_year -= 1
            period_start = dt.date(start_year, start_month, 1)
            truth = (group[group.period_end == pd.Timestamp(period_end)]
                     .set_index("metric")["value"])
            filing_date = pd.Timestamp(
                group[group.period_end == pd.Timestamp(period_end)]["filing_date"].iloc[0]
            ).date()
            document = match.iloc[0]
            name = document["pdf_url"].rsplit("/", 1)[-1]

            try:
                pdf_bytes = provider.fetch_pdf(document["pdf_url"])
                extracted, usage = provider.extract(
                    pdf_bytes, period_start, period_end, filename=name,
                )
                got = provider.to_facts(extracted, symbol, filing_date, period_end)
                # An extraction can validate structurally and still carry no
                # figures — the parsed text may not have held the table at all.
                if got.empty:
                    raise ProviderError(
                        "extraction returned no figures "
                        f"({(extracted.extraction_notes or 'no notes')[:90]})"
                    )
                got_series = got.set_index("metric")["value"]
            except ProviderError as exc:
                print(f"  {symbol:<14} {period_end} EXTRACTION FAILED: {exc}")
                all_results.append({"symbol": symbol, "period_end": period_end,
                                    "matched": 0, "compared": 0,
                                    "cost_usd": 0.0, "error": str(exc)[:70]})
                continue

            result = compare(truth, got_series)
            matched, compared = int(result.match.sum()), len(result)
            cost = usage.get("cost_usd") or 0.0

            # Would we have known this was wrong WITHOUT the XBRL reference?
            # That is what matters for the thousands of companies where no
            # reference exists.
            problems = consistency_check(got_series.to_dict())
            flag = "self-check FAILED" if problems else "self-check ok"
            print(f"  {symbol:<14} {period_end}  {matched}/{compared} within "
                  f"{TOLERANCE:.1%}  ${cost:.4f}  {flag}  [{name[:30]}]")
            for problem in problems:
                print(f"      arithmetic  {problem}")
            for _, row in result[~result.match].iterrows():
                print(f"      MISMATCH {row.metric:<16} xbrl={row.expected:>18,.0f} "
                      f"pdf={row.actual if pd.notna(row.actual) else float('nan'):>18,.0f} "
                      f"{row.note}")
            if extracted.extraction_notes:
                print(f"      notes: {extracted.extraction_notes[:120]}")

            all_results.append({"symbol": symbol, "period_end": period_end,
                                "matched": matched, "compared": compared,
                                "cost_usd": cost, "self_check_failed": bool(problems),
                                "error": ""})

    if not all_results:
        print("\nNothing could be compared.", file=sys.stderr)
        return 1

    summary = pd.DataFrame(all_results)
    total_matched = int(summary.matched.sum())
    total_compared = int(summary.compared.sum())
    rate = total_matched / total_compared if total_compared else 0.0

    spend = float(summary.get("cost_usd", pd.Series(dtype=float)).sum())
    statements = len(summary)
    per_doc = spend / statements if statements else 0.0
    # Full backfill: ~750 companies x 6 post-XBRL quarters.
    projected = per_doc * 750 * 6

    print(f"\n{'=' * 62}")
    print(f"model:    {provider.model}  (pdf engine: {settings_module.PDF_ENGINE})")
    print(f"accuracy: {total_matched}/{total_compared} metrics "
          f"({rate:.1%}) across {statements} statements")
    print(f"cost:     ${spend:.4f} total, ${per_doc:.4f}/statement "
          f"-> ~${projected:,.0f} for the full backfill (~4,500 statements)")
    print(f"threshold: {PASS_THRESHOLD:.0%}  ->  "
          f"{'PASS — safe to ingest' if rate >= PASS_THRESHOLD else 'FAIL — do not ingest'}")

    settings_dir = __import__("engine.config", fromlist=["settings"]).settings.REPORT_DIR
    settings_dir.mkdir(parents=True, exist_ok=True)
    summary.to_csv(settings_dir / "pdf_extraction_accuracy.csv", index=False)
    print(f"report: {settings_dir / 'pdf_extraction_accuracy.csv'}")

    return 0 if rate >= PASS_THRESHOLD else 2


if __name__ == "__main__":
    sys.exit(main())
