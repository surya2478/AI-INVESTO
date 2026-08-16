"""Falsifiable thesis claims, and the evidence that tests them.

WHY THIS EXISTS
---------------
A position was opened with a reason written in prose, and nothing ever checked
it. `review_thesis` tested critical gates, any gate failure, and the score band
-- all generic, none of them the reason anybody actually bought. WABAG's thesis
rests on a "policy-visible order book from AMRUT 2.0 and ZLD mandates"; the
engine has extracted order books from BSE announcements the whole time and the
two were never connected. So a reason could stop being true in silence, which is
where most retail money is lost -- not on a gate nobody noticed, but on holding
long after the thing you believed stopped happening.

A claim is one falsifiable assertion: a metric, a direction, a threshold.
`{"metric": "order_book_to_sales", "comparator": ">=", "threshold": 2.0}` reads
as "I believe the order book stays at least twice revenue, and if it does not, I
was wrong."

THREE OUTCOMES, NOT TWO
-----------------------
HOLDS, BROKEN and UNCHECKABLE. The third is the honest one and it is reported
rather than hidden: a claim whose input has no data is not a claim that passed.
Knowing which of your reasons cannot currently be tested is itself worth having,
because it tells you which parts of a thesis are faith rather than evidence.

Everything is measured as of a date through the point-in-time read path, so a
claim checked against a historical date sees only what was public then.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

HOLDS, BROKEN, UNCHECKABLE = "HOLDS", "BROKEN", "UNCHECKABLE"
COMPARATORS = (">=", "<=")


@dataclass
class Claim:
    metric: str
    comparator: str
    threshold: float
    note: str = ""
    claim_id: int | None = None

    def describe(self) -> str:
        return f"{self.metric} {self.comparator} {self.threshold:g}"


@dataclass
class ClaimCheck:
    claim: Claim
    status: str
    observed: float | None
    detail: str


# --------------------------------------------------------------- measurements
# Each entry answers one question about one company as of one date. Kept small
# and explicit: a claim the engine cannot measure should fail to register rather
# than silently evaluate against something adjacent.
def _annual(facts: pd.DataFrame, metric: str) -> pd.Series:
    rows = facts[(facts.period_type == "A") & (facts.metric == metric)]
    if rows.empty:
        return pd.Series(dtype="float64")
    return (rows.sort_values("period_end")
            .drop_duplicates("period_end", keep="last")
            .set_index("period_end")["value"].astype(float))


def _revenue_growth_yoy(facts, **_) -> float | None:
    revenue = _annual(facts, "revenue")
    if len(revenue) < 2 or revenue.iloc[-2] <= 0:
        return None
    return (revenue.iloc[-1] / revenue.iloc[-2] - 1.0) * 100.0


def _revenue_cagr_2y(facts, **_) -> float | None:
    revenue = _annual(facts, "revenue")
    if len(revenue) < 3 or revenue.iloc[-3] <= 0 or revenue.iloc[-1] <= 0:
        return None
    return ((revenue.iloc[-1] / revenue.iloc[-3]) ** 0.5 - 1.0) * 100.0


def _ebitda_margin(facts, **_) -> float | None:
    ebitda, revenue = _annual(facts, "ebitda"), _annual(facts, "revenue")
    if ebitda.empty or revenue.empty or revenue.iloc[-1] <= 0:
        return None
    return float(ebitda.iloc[-1] / revenue.iloc[-1]) * 100.0


def _roe(facts, **_) -> float | None:
    pat, net_worth = _annual(facts, "pat"), _annual(facts, "net_worth")
    if pat.empty or net_worth.empty or net_worth.iloc[-1] <= 0:
        return None
    return float(pat.iloc[-1] / net_worth.iloc[-1]) * 100.0


def _debt_equity(facts, **_) -> float | None:
    debt, net_worth = _annual(facts, "total_debt"), _annual(facts, "net_worth")
    if debt.empty or net_worth.empty or net_worth.iloc[-1] <= 0:
        return None
    return float(debt.iloc[-1] / net_worth.iloc[-1])


def _promoter_pct(_, ownership=None, **__) -> float | None:
    if ownership is None or ownership.empty:
        return None
    latest = ownership.sort_values(["quarter_end", "filing_date"]).iloc[-1]
    value = latest.get("promoter_pct")
    return None if pd.isna(value) else float(value)


def _promoter_pledge_pct(_, ownership=None, **__) -> float | None:
    if ownership is None or ownership.empty:
        return None
    latest = ownership.sort_values(["quarter_end", "filing_date"]).iloc[-1]
    value = latest.get("promoter_pledge_pct")
    return None if pd.isna(value) else float(value)


def _order_book_to_sales(_, orders=None, **__) -> float | None:
    """The claim WABAG's thesis actually rests on, finally connected."""
    if not orders:
        return None
    value = orders.get("book_to_sales")
    return None if value is None else float(value)


MEASURES = {
    "revenue_growth_yoy": (_revenue_growth_yoy, "%", "latest annual revenue growth"),
    "revenue_cagr_2y": (_revenue_cagr_2y, "%", "2-year revenue CAGR"),
    "ebitda_margin": (_ebitda_margin, "%", "latest annual EBITDA margin"),
    "roe": (_roe, "%", "latest annual return on equity"),
    "debt_equity": (_debt_equity, "x", "total debt to net worth"),
    "promoter_pct": (_promoter_pct, "%", "promoter holding"),
    "promoter_pledge_pct": (_promoter_pledge_pct, "%", "pledge as a share of promoter holding"),
    "order_book_to_sales": (_order_book_to_sales, "x", "announced order book to revenue"),
}


def measure(analytics_con, ticker: str, as_of: dt.date | None = None) -> dict:
    """Every measurable quantity for one company, as of a date.

    Read through the point-in-time path, so a claim checked against a past date
    sees only what had been published by then.
    """
    from engine.orders import company_orders
    from engine.storage import db

    as_of = as_of or dt.date.today()
    security_id = db.resolve_ticker(analytics_con, ticker)
    if security_id is None:
        return {}

    facts = db.fundamentals_asof(analytics_con, as_of, security_ids=[security_id],
                                 periods=16, include_non_pit=True)
    ownership = analytics_con.execute("""
        SELECT quarter_end, filing_date, promoter_pct, promoter_pledge_pct
          FROM ownership_pit WHERE security_id = ? AND filing_date <= ?
    """, [security_id, as_of]).df()

    try:
        orders = company_orders(analytics_con, ticker, as_of=as_of)
    except Exception:  # noqa: BLE001 - a missing order book is not a failure
        orders = {}

    if facts.empty:
        facts = pd.DataFrame(columns=["period_type", "metric", "period_end", "value"])

    return {
        name: fn(facts, ownership=ownership, orders=orders)
        for name, (fn, _unit, _label) in MEASURES.items()
    }


def check(claims: list[Claim], measured: dict) -> list[ClaimCheck]:
    """Test each claim against what was measured.

    A claim whose metric has no value is UNCHECKABLE, never HOLDS. Absence of
    evidence is the one thing this module exists to stop reading as support.
    """
    out = []
    for claim in claims:
        _fn, unit, label = MEASURES.get(claim.metric, (None, "", claim.metric))
        observed = measured.get(claim.metric)

        if observed is None:
            out.append(ClaimCheck(claim, UNCHECKABLE, None,
                                  f"{label} is not measurable from current data"))
            continue

        holds = (observed >= claim.threshold if claim.comparator == ">="
                 else observed <= claim.threshold)
        out.append(ClaimCheck(
            claim, HOLDS if holds else BROKEN, observed,
            f"{label} {observed:,.1f}{unit} "
            f"({'still' if holds else 'no longer'} {claim.comparator} "
            f"{claim.threshold:g}{unit})",
        ))
    return out


def health_from_checks(checks: list[ClaimCheck]) -> tuple[str | None, list[str]]:
    """What the claims alone say about a position.

    None means the claims say nothing -- no claims recorded, or none checkable.
    Broken claims are the thesis breaking, which is the exit trigger this book
    was designed around; HALF OR MORE of the checkable claims broken is RED
    rather than AMBER, because at that point the reason for holding has mostly
    gone. Uncheckable claims are excluded from the denominator: they are neither
    evidence for nor against.
    """
    broken = [c for c in checks if c.status == BROKEN]
    checkable = [c for c in checks if c.status != UNCHECKABLE]
    if not checkable:
        return None, []

    reasons = [f"Thesis claim broken: {c.detail}" for c in broken]
    if not broken:
        return "GREEN", []
    return ("RED" if len(broken) * 2 >= len(checkable) else "AMBER"), reasons
