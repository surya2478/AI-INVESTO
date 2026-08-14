"""Quality gates -- the graveyard filter.

Most retail multibagger hunting dies on accounting and governance failures, not
on stock selection, so these run before scoring and can reject a name outright
however good its momentum looks.

TRI-STATE BY DESIGN
-------------------
Every gate returns PASS, FAIL or UNKNOWN. UNKNOWN is not a pass. A company we
cannot evaluate for promoter pledge is not a company that has no pledge, and
collapsing the two would quietly present unvetted names as clean. `gate_summary`
therefore reports cleared and unknown counts separately, and the app is expected
to show both.

Several gates from the spec need data we do not hold yet -- quarterly results
carry no cash-flow or balance-sheet detail, so cash conversion, receivable days,
related-party share and contingent liabilities all return UNKNOWN with the
reason stated. They are declared here rather than omitted so the gap is visible
in the output instead of being invisible by absence.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from engine.config import settings
from engine.storage import db

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"


@dataclass
class GateResult:
    name: str
    status: str
    observed: float | None = None
    threshold: float | None = None
    detail: str = ""


@dataclass
class GateContext:
    """Everything a gate needs, assembled once per company."""
    security_id: int
    ticker: str
    as_of: dt.date
    quarterly: pd.DataFrame        # metric x period_end, point-in-time filtered
    prices: pd.DataFrame           # date, close, volume
    events: pd.DataFrame           # corporate_events on or before as_of
    market_cap: float | None       # rupees
    ownership: pd.DataFrame | None = None   # promoter holding and pledge

    def series(self, metric: str) -> pd.Series:
        """Quarterly values for one metric, oldest first."""
        if self.quarterly.empty or metric not in self.quarterly.columns:
            return pd.Series(dtype="float64")
        return self.quarterly[metric].dropna().sort_index()

    def ttm(self, metric: str, offset: int = 0) -> float | None:
        """Trailing four quarters, optionally ending `offset` quarters back."""
        values = self.series(metric)
        end = len(values) - offset
        if end < 4:
            return None
        return float(values.iloc[end - 4:end].sum())


# --------------------------------------------------------------------- gates
def gate_surveillance(ctx: GateContext) -> GateResult:
    """NSE has flagged unusual price or volume behaviour in this name."""
    if ctx.events.empty:
        return GateResult("surveillance", PASS, detail="No surveillance flags on record")
    flagged = ctx.events[ctx.events.event_type == "ASM_SURVEILLANCE"]
    if flagged.empty:
        return GateResult("surveillance", PASS, detail="No surveillance flags on record")
    latest = flagged.sort_values("event_date").iloc[-1]
    return GateResult(
        "surveillance", FAIL,
        detail=f"Under NSE surveillance since {latest.event_date} ({latest.detail})",
    )


def gate_liquidity(ctx: GateContext) -> GateResult:
    """Enough daily turnover to build and exit a position without moving the price."""
    if ctx.prices.empty or len(ctx.prices) < 30:
        return GateResult("liquidity", UNKNOWN, detail="Fewer than 30 trading days of history")

    recent = ctx.prices.tail(60)
    turnover_cr = float((recent["close"] * recent["volume"]).median() / 1e7)
    floor = settings.MIN_DAILY_TURNOVER_CR
    if turnover_cr < floor:
        return GateResult("liquidity", FAIL, turnover_cr, floor,
                          f"Median daily turnover Rs {turnover_cr:.2f} cr is below the "
                          f"Rs {floor:.2f} cr floor")
    return GateResult("liquidity", PASS, turnover_cr, floor,
                      f"Median daily turnover Rs {turnover_cr:.1f} cr")


def gate_serial_dilution(ctx: GateContext) -> GateResult:
    """Repeated share issuance transfers value away from existing holders."""
    capital = ctx.series("equity_capital")
    if len(capital) < 8:
        return GateResult("serial_dilution", UNKNOWN,
                          detail="Needs 8 quarters of equity capital history")
    then, now = float(capital.iloc[-8]), float(capital.iloc[-1])
    if then <= 0:
        return GateResult("serial_dilution", UNKNOWN, detail="No usable base period")

    growth = (now / then - 1.0) * 100.0
    if growth > 25.0:
        return GateResult("serial_dilution", FAIL, growth, 25.0,
                          f"Share capital up {growth:.0f}% over 2 years")
    return GateResult("serial_dilution", PASS, growth, 25.0,
                      f"Share capital {growth:+.0f}% over 2 years")


def gate_interest_coverage(ctx: GateContext) -> GateResult:
    """Can operating profit service the debt?"""
    pbt, finance = ctx.ttm("pbt"), ctx.ttm("finance_cost")
    if pbt is None or finance is None:
        return GateResult("interest_coverage", UNKNOWN, detail="Needs 4 quarters of results")
    if finance <= 0:
        return GateResult("interest_coverage", PASS, detail="No meaningful interest burden")

    coverage = (pbt + finance) / finance
    if coverage < 1.5:
        return GateResult("interest_coverage", FAIL, coverage, 1.5,
                          f"Operating profit covers interest only {coverage:.1f}x")
    return GateResult("interest_coverage", PASS, coverage, 1.5,
                      f"Interest covered {coverage:.1f}x")


def gate_sustained_losses(ctx: GateContext) -> GateResult:
    """A pre-inflection company can lose money; a broken one loses it every quarter."""
    pat = ctx.series("pat")
    if len(pat) < 4:
        return GateResult("sustained_losses", UNKNOWN, detail="Needs 4 quarters of results")
    losses = int((pat.tail(4) < 0).sum())
    if losses >= 3:
        return GateResult("sustained_losses", FAIL, losses, 3.0,
                          f"Loss-making in {losses} of the last 4 quarters")
    return GateResult("sustained_losses", PASS, losses, 3.0,
                      f"{losses} loss-making quarter(s) in the last 4")


def gate_revenue_collapse(ctx: GateContext) -> GateResult:
    """A shrinking top line contradicts a growth thesis outright."""
    now, prior = ctx.ttm("revenue"), ctx.ttm("revenue", offset=4)
    if now is None or prior is None or prior <= 0:
        return GateResult("revenue_collapse", UNKNOWN, detail="Needs 8 quarters of revenue")
    change = (now / prior - 1.0) * 100.0
    if change < -25.0:
        return GateResult("revenue_collapse", FAIL, change, -25.0,
                          f"Trailing revenue down {abs(change):.0f}% year on year")
    return GateResult("revenue_collapse", PASS, change, -25.0,
                      f"Trailing revenue {change:+.0f}% year on year")


def gate_promoter_pledge(ctx: GateContext) -> GateResult:
    """Promoters who have borrowed against their stake.

    A pledged promoter holding is a forced-seller in waiting: if the price
    falls, the lender sells, which drives the price down further. It is one of
    the most reliable precursors of a permanent loss in Indian small caps, and
    it says nothing about the business -- which is why it is a gate rather than
    a score input.

    Measured against PROMOTER HOLDING, not total equity. See
    NSEProvider.fetch_promoter_pledge for why the distinction matters.
    """
    if ctx.ownership is None or ctx.ownership.empty:
        return GateResult("promoter_pledge", UNKNOWN, detail="No shareholding disclosure on record")

    latest = ctx.ownership.sort_values("quarter_end").iloc[-1]
    pledge = latest.get("promoter_pledge_pct")
    if pledge is None or pd.isna(pledge):
        return GateResult("promoter_pledge", UNKNOWN, detail="Disclosure carries no pledge figure")

    pledge = float(pledge)
    promoter = latest.get("promoter_pct")
    holding = f", promoters hold {float(promoter):.1f}%" if pd.notna(promoter) else ""

    if pledge > 20.0:
        return GateResult("promoter_pledge", FAIL, pledge, 20.0,
                          f"{pledge:.1f}% of the promoter stake is pledged{holding}")
    return GateResult("promoter_pledge", PASS, pledge, 20.0,
                      f"{pledge:.1f}% of the promoter stake is pledged{holding}")


def gate_market_cap_band(ctx: GateContext) -> GateResult:
    """Too small to trade safely, or too large to multiply."""
    if ctx.market_cap is None or ctx.market_cap <= 0:
        return GateResult("market_cap_band", UNKNOWN, detail="No market cap on record")
    crores = ctx.market_cap / 1e7
    low, high = settings.MIN_MARKET_CAP_CR, settings.MAX_MARKET_CAP_CR
    if crores < low:
        return GateResult("market_cap_band", FAIL, crores, low,
                          f"Market cap Rs {crores:,.0f} cr is below the Rs {low:,.0f} cr floor")
    if crores > high:
        return GateResult("market_cap_band", FAIL, crores, high,
                          f"Market cap Rs {crores:,.0f} cr is above the Rs {high:,.0f} cr ceiling "
                          "for an early-stage position")
    return GateResult("market_cap_band", PASS, crores, high, f"Market cap Rs {crores:,.0f} cr")


# Gates the spec calls for that current data cannot answer. Declared so the gap
# shows up in every report rather than being invisible by omission.
def _needs(name: str, reason: str) -> Callable[[GateContext], GateResult]:
    def gate(ctx: GateContext) -> GateResult:
        return GateResult(name, UNKNOWN, detail=reason)
    return gate


GATES: list[Callable[[GateContext], GateResult]] = [
    gate_surveillance,
    gate_liquidity,
    gate_serial_dilution,
    gate_interest_coverage,
    gate_sustained_losses,
    gate_revenue_collapse,
    gate_market_cap_band,
    gate_promoter_pledge,
    _needs("cash_conversion", "Needs cash-flow statements; quarterly results omit them"),
    _needs("receivable_days", "Needs balance-sheet detail; quarterly results omit it"),
    _needs("related_party", "Needs annual report extraction"),
    _needs("contingent_liabilities", "Needs annual report extraction"),
]

# A FAIL on any of these rejects the name outright. The rest inform the score.
CRITICAL = {"surveillance", "cash_conversion", "serial_dilution", "sustained_losses"}


# ----------------------------------------------------------------- execution
def build_context(con, security_id: int, ticker: str, as_of: dt.date) -> GateContext:
    # include_non_pit=True: gates screen what is true TODAY, so Yahoo's
    # current-value figures are exactly the right input. The Stage 3 backtest
    # must call fundamentals_asof with the default and see filing-dated rows
    # only -- that is the whole reason the two are separated.
    facts = db.fundamentals_asof(con, as_of, security_ids=[security_id],
                                 periods=16, include_non_pit=True)
    quarterly = pd.DataFrame()
    if not facts.empty:
        q = facts[facts.period_type == "Q"]
        if not q.empty:
            quarterly = q.pivot_table(index="period_end", columns="metric",
                                      values="value", aggfunc="last").sort_index()

    prices = con.execute("""
        SELECT date, adj_close AS close, volume
          FROM ohlcv WHERE security_id = ? AND date <= ?
         ORDER BY date
    """, [security_id, as_of]).df()

    events = con.execute("""
        SELECT event_date, event_type, detail, severity
          FROM corporate_events WHERE security_id = ? AND event_date <= ?
    """, [security_id, as_of]).df()

    cap = con.execute(
        "SELECT market_cap FROM securities WHERE security_id = ?", [security_id]
    ).fetchone()

    ownership = con.execute("""
        SELECT quarter_end, filing_date, promoter_pct, promoter_pledge_pct, public_pct
          FROM ownership_pit
         WHERE security_id = ? AND filing_date <= ?
    """, [security_id, as_of]).df()

    return GateContext(
        security_id=security_id, ticker=ticker, as_of=as_of,
        quarterly=quarterly, prices=prices, events=events,
        market_cap=float(cap[0]) if cap and cap[0] else None,
        ownership=ownership,
    )


def evaluate(con, security_id: int, ticker: str, as_of: dt.date) -> list[GateResult]:
    ctx = build_context(con, security_id, ticker, as_of)
    return [gate(ctx) for gate in GATES]


def run_gates(con, as_of: dt.date | None = None, limit: int = 0) -> pd.DataFrame:
    """Evaluate every gate for every priced security and persist the results."""
    as_of = as_of or dt.date.today()

    query = """
        SELECT DISTINCT s.security_id, s.ticker
          FROM securities s JOIN ohlcv o ON o.security_id = s.security_id
         WHERE s.country = 'IN'
         ORDER BY s.ticker
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    targets = con.execute(query).df()

    rows = []
    for record in targets.itertuples():
        for result in evaluate(con, record.security_id, record.ticker, as_of):
            rows.append({
                "security_id": record.security_id,
                "ticker": record.ticker,
                "as_of_date": as_of,
                "gate_name": result.name,
                "status": result.status,
                "passed": result.status == PASS,
                "observed_value": result.observed,
                "threshold": result.threshold,
                "detail": result.detail,
            })

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    con.register("staged_gates", frame[[
        "security_id", "as_of_date", "gate_name", "status", "passed",
        "observed_value", "threshold", "detail",
    ]])
    con.execute("""
        DELETE FROM gate_results
         WHERE (security_id, as_of_date, gate_name) IN (
               SELECT security_id, as_of_date, gate_name FROM staged_gates)
    """)
    con.execute("""
        INSERT INTO gate_results
            (security_id, as_of_date, gate_name, status, passed,
             observed_value, threshold, detail)
        SELECT security_id, as_of_date, gate_name, status, passed,
               observed_value, threshold, detail
          FROM staged_gates
    """)
    con.unregister("staged_gates")
    return frame


def gate_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-gate outcome counts, keeping UNKNOWN visibly distinct from PASS."""
    if frame.empty:
        return frame
    return (
        frame.pivot_table(index="gate_name", columns="status", values="ticker",
                          aggfunc="count", fill_value=0)
        .reindex(columns=[PASS, FAIL, UNKNOWN], fill_value=0)
        .sort_values(FAIL, ascending=False)
    )


def verdicts(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-company verdict: REJECTED, UNVETTED or CLEARED.

    UNVETTED exists so a name that merely lacks data is never shown alongside
    one that has actually been checked and cleared.
    """
    if frame.empty:
        return frame

    rows = []
    for ticker, group in frame.groupby("ticker"):
        failed = group[group.status == FAIL]
        critical_fail = failed[failed.gate_name.isin(CRITICAL)]
        unknown_critical = group[(group.status == UNKNOWN)
                                 & (group.gate_name.isin(CRITICAL))]

        if not critical_fail.empty:
            verdict = "REJECTED"
            reason = "; ".join(critical_fail.detail.head(2))
        elif not failed.empty:
            verdict = "FLAGGED"
            reason = "; ".join(failed.detail.head(2))
        elif not unknown_critical.empty:
            verdict = "UNVETTED"
            reason = f"{len(unknown_critical)} critical gate(s) not evaluable"
        else:
            verdict = "CLEARED"
            reason = ""

        rows.append({
            "ticker": ticker, "verdict": verdict,
            "failed": int(len(failed)),
            "unknown": int((group.status == UNKNOWN).sum()),
            "reason": reason,
        })

    order = {"REJECTED": 0, "FLAGGED": 1, "UNVETTED": 2, "CLEARED": 3}
    return (pd.DataFrame(rows)
            .assign(_o=lambda d: d.verdict.map(order))
            .sort_values(["_o", "ticker"]).drop(columns="_o")
            .reset_index(drop=True))
