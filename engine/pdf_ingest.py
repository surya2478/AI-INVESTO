"""Batch ingest of PDF-extracted quarterly financials.

Runs in bounded batches so quality is visible before more of it exists. Two
rules make that meaningful:

  * ONLY guard-clean extractions reach `fundamentals_pit`. Anything that fails
    the period echo, the arithmetic identities, or the adjacent-column test is
    written to `pdf_extractions` with the reason and goes no further. A suspect
    figure sitting in the scoring table is worse than a missing one, because
    nothing downstream can tell it apart from a good one.
  * Every attempt is recorded either way, so the clean rate per batch is a
    measured number rather than an impression.

The measured clean rate on the validation sample was 14 of 28 statements
perfect with 12 of the remaining 14 flagged, so expect roughly half of all
attempts to be quarantined at first. That is the guards working, not the
pipeline failing.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import pandas as pd

from engine.config import settings
from engine.providers.base import ProviderError
from engine.providers.pdf_extractor import (
    BSEPDFProvider,
    InsufficientCredit,
    adjacent_column_check,
    consistency_check,
)
from engine.storage import db

log = logging.getLogger(__name__)

# XBRL covers everything through Dec-2024; PDFs are only needed after that.
PDF_ERA_START = dt.date(2025, 1, 1)
BATCH_JOB = "pdf_backfill"


def candidate_companies(con, batch_size: int, restart: bool = False,
                        themes_only: bool = False) -> pd.DataFrame:
    """Next companies to process, theme members first.

    Two deliberate choices, both learned from the first batch:

    * FINANCIALS EXCLUDED. Banks and NBFCs report interest earned and expended,
      not revenue from operations, so neither the extraction schema nor the
      arithmetic identities describe their statements. The guards correctly
      rejected almost every one. They are also irrelevant to a thesis built on
      solar, grid, water, semiconductors and health.
    * THEME MEMBERS FIRST. Alphabetical order spent the first batch on 360ONE
      and Aadhar Housing Finance. Ordering by theme membership means the
      companies the score actually ranks are populated first, so the data
      becomes useful long before the backfill finishes.
    """
    from engine.universe.theme_graph import load_theme_graph

    processed = set(
        con.execute("SELECT DISTINCT security_id FROM pdf_extractions").df()["security_id"]
    ) if not restart else set()

    theme_tickers = set(load_theme_graph().india_universe())

    # Theme members are included whether or not they are in the index. 14 of
    # them sit below the NIFTY TOTAL MARKET cutoff, and drawing only from the
    # index would silently skip companies deliberately chosen for the thesis --
    # which is also where early-stage names are most likely to be.
    placeholders = ",".join("?" * len(theme_tickers)) or "''"
    frame = con.execute(f"""
        SELECT DISTINCT s.security_id, s.ticker, s.exchange_symbol,
               coalesce(s.industry, '') AS industry
          FROM securities s
          LEFT JOIN index_membership im
                 ON im.security_id = s.security_id
                AND im.index_name = 'NIFTY TOTAL MARKET'
                AND im.to_date IS NULL
         WHERE s.country = 'IN'
           AND (im.security_id IS NOT NULL OR s.ticker IN ({placeholders}))
           AND (coalesce(s.industry, '') NOT IN ('Financial Services', 'Insurance')
                OR s.ticker IN ({placeholders}))
         ORDER BY s.ticker
    """, list(theme_tickers) * 2).df()

    frame = frame[~frame["security_id"].isin(processed)].copy()
    frame["priority"] = frame["ticker"].map(lambda t: 0 if t in theme_tickers else 1)
    if themes_only:
        frame = frame[frame["priority"] == 0]
    return (frame.sort_values(["priority", "ticker"])
            .head(batch_size)
            .drop(columns=["industry", "priority"])
            .reset_index(drop=True))


def prior_quarter_values(con, security_id: int, period_end: dt.date) -> dict:
    """Most recent stored quarter before `period_end`, for the duplicate check."""
    frame = con.execute("""
        SELECT metric, value FROM fundamentals_pit
         WHERE security_id = ? AND period_type = 'Q' AND period_end < ?
           AND period_end = (
               SELECT max(period_end) FROM fundamentals_pit
                WHERE security_id = ? AND period_type = 'Q' AND period_end < ?
           )
    """, [security_id, period_end, security_id, period_end]).df()
    return dict(zip(frame["metric"], frame["value"])) if not frame.empty else {}


def process_company(con, provider, security_id: int, symbol: str) -> list[dict]:
    """Extract every post-XBRL quarter for one company."""
    # Broad catch on purpose: a network timeout is not a ProviderError, and one
    # unreachable company must not end a batch that has already spent money.
    try:
        documents = provider.fetch_result_documents(symbol)
    except Exception as exc:  # noqa: BLE001
        return [{"symbol": symbol, "status": "FAILED",
                 "problems": f"{type(exc).__name__}: {exc}"[:140],
                 "period_end": None, "cost": 0.0}]

    if documents.empty:
        return [{"symbol": symbol, "status": "FAILED", "problems": "no result documents",
                 "period_end": None, "cost": 0.0}]

    already = set(con.execute("""
        SELECT period_end FROM pdf_extractions WHERE security_id = ? AND model = ?
    """, [security_id, provider.model]).df()["period_end"].tolist())

    outcomes = []
    for record in documents.itertuples():
        period_end = record.period_end
        if period_end < PDF_ERA_START or period_end in already:
            continue

        start_month = period_end.month - 2
        start_year = period_end.year
        if start_month <= 0:
            start_month += 12
            start_year -= 1
        period_start = dt.date(start_year, start_month, 1)

        outcome = {"symbol": symbol, "period_end": period_end, "cost": 0.0}
        try:
            pdf_bytes = provider.fetch_pdf(record.pdf_url)
            extracted, usage = provider.extract(
                pdf_bytes, period_start, period_end,
                filename=record.pdf_url.rsplit("/", 1)[-1],
            )
            facts = provider.to_facts(extracted, symbol, record.filing_date, period_end)
            outcome["cost"] = usage.get("cost_usd") or 0.0
        except InsufficientCredit:
            raise          # abort the batch; nothing is wrong with the statement
        except Exception as exc:  # noqa: BLE001 - see note above
            outcome.update(status="FAILED", problems=f"{type(exc).__name__}: {exc}"[:180])
            outcomes.append(outcome)
            _record_attempt(con, security_id, period_end, provider.model, outcome,
                            record.filing_date, record.pdf_url, None)
            continue

        if facts.empty:
            outcome.update(status="FAILED", problems="no figures extracted")
            outcomes.append(outcome)
            _record_attempt(con, security_id, period_end, provider.model, outcome,
                            record.filing_date, record.pdf_url, None)
            continue

        values = dict(zip(facts["metric"], facts["value"]))
        problems = consistency_check(values)
        duplicate = adjacent_column_check(
            values, prior_quarter_values(con, security_id, period_end)
        )
        if duplicate:
            problems.append(duplicate)

        outcome["status"] = "QUARANTINED" if problems else "CLEAN"
        outcome["problems"] = "; ".join(problems)[:400]
        _record_attempt(con, security_id, period_end, provider.model, outcome,
                        record.filing_date, record.pdf_url, values)

        if not problems:
            _write_facts(con, security_id, facts)

        outcomes.append(outcome)

    return outcomes


def _record_attempt(con, security_id, period_end, model, outcome,
                    filing_date, pdf_url, values) -> None:
    con.execute("""
        INSERT OR REPLACE INTO pdf_extractions
            (security_id, period_end, model, status, problems, filing_date,
             pdf_url, cost_usd, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [security_id, period_end, model, outcome.get("status"),
          outcome.get("problems"), filing_date, pdf_url,
          outcome.get("cost", 0.0), json.dumps(values, default=str) if values else None])


def _write_facts(con, security_id: int, facts: pd.DataFrame) -> None:
    staged = pd.DataFrame({
        "security_id": security_id,
        "period_end": facts["period_end"],
        "period_type": facts["period_type"],
        "filing_date": facts["filing_date"],
        "metric": facts["metric"],
        "value": pd.to_numeric(facts["value"], errors="coerce"),
        "unit": facts["unit"],
        "source": "pdf_llm",
    }).dropna(subset=["value"])

    con.register("staged_pdf_facts", staged)
    con.execute("""
        INSERT INTO fundamentals_pit
            (security_id, period_end, period_type, filing_date, metric, value, unit, source)
        SELECT s.security_id, s.period_end, s.period_type, s.filing_date,
               s.metric, s.value, s.unit, s.source
          FROM staged_pdf_facts s
         WHERE NOT EXISTS (
               SELECT 1 FROM fundamentals_pit f
                WHERE f.security_id = s.security_id AND f.period_end = s.period_end
                  AND f.period_type = s.period_type AND f.metric = s.metric
                  AND f.filing_date = s.filing_date
         )
    """)
    con.unregister("staged_pdf_facts")


def run_batch(con, batch_size: int = 10, model: str | None = None,
              restart: bool = False, themes_only: bool = False,
              progress=None) -> dict:
    """Process one batch and report its quality."""
    provider = BSEPDFProvider(model=model)
    companies = candidate_companies(con, batch_size, restart, themes_only)
    if companies.empty:
        return {"companies": 0, "message": "backfill complete — no companies left"}

    started = dt.datetime.now()
    outcomes: list[dict] = []
    aborted = None
    for index, record in enumerate(companies.itertuples(), 1):
        try:
            results = process_company(con, provider, record.security_id,
                                      record.exchange_symbol)
        except InsufficientCredit as exc:
            aborted = str(exc)
            break
        outcomes.extend(results)
        if progress:
            clean = sum(1 for r in results if r.get("status") == "CLEAN")
            progress(index, len(companies), record.exchange_symbol, clean, len(results))
        con.execute("DELETE FROM job_state WHERE job = ?", [BATCH_JOB])
        con.execute("""
            INSERT INTO job_state (job, cursor_date, detail, updated_at)
            VALUES (?, NULL, ?, current_timestamp)
        """, [BATCH_JOB, record.ticker])

    frame = pd.DataFrame(outcomes)
    counts = frame["status"].value_counts().to_dict() if not frame.empty else {}
    attempted = int(len(frame[frame.period_end.notna()])) if not frame.empty else 0
    clean = counts.get("CLEAN", 0)

    return {
        "aborted": aborted,
        "companies": len(companies),
        "attempted": attempted,
        "clean": clean,
        "quarantined": counts.get("QUARANTINED", 0),
        "failed": counts.get("FAILED", 0),
        "clean_rate": clean / attempted if attempted else 0.0,
        "cost": float(frame["cost"].sum()) if not frame.empty else 0.0,
        "seconds": (dt.datetime.now() - started).total_seconds(),
        "last_ticker": companies["ticker"].iloc[-1],
        "problems": frame[frame.problems.notna() & (frame.problems != "")]
                    if not frame.empty else pd.DataFrame(),
    }


def quality_report(con, model: str | None = None) -> pd.DataFrame:
    """Cumulative outcome counts, so drift across batches is visible."""
    model = model or settings.LLM_MODEL
    return con.execute("""
        SELECT status, count(*) AS statements,
               count(DISTINCT security_id) AS companies,
               round(sum(cost_usd), 4) AS cost_usd
          FROM pdf_extractions WHERE model = ?
         GROUP BY status ORDER BY statements DESC
    """, [model]).df()
