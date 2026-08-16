"""Do the gates predict anything? THE ANSWER THIS PRODUCES IS INCONCLUSIVE.

Kept because the machinery is correct and worth re-running once the data can
support it -- not because the output means anything today. Read the gates
section of docs/BACKTEST.md before quoting a number from here.

Two reasons the result cannot be read as evidence:

  * The gates exist to avoid PERMANENT LOSS, and the universe is today's index
    membership, so every company that failed or delisted is already absent. The
    outcome they guard against has been deleted from the sample.
  * CLEARED needs no critical gate UNKNOWN, and cash conversion needs annual
    cash flow, which point-in-time begins at FY2022. Before 2024 every company
    is UNVETTED by construction, leaving two usable dates.

Note also what NOT to do with the output: the pooled by-verdict table compares
CLEARED (which exists only in 2024-25, flat years) against REJECTED (dominated
by 2020-23, including a 200% rebound). That compares regimes, not verdicts.
Per-date is the only valid read.

Evaluated point-in-time at each rebalance date -- include_non_pit=False so the
gates see only filing-dated rows, and market cap reconstructed rather than read
from today's securities table -- then compared against 12-month forward returns.

    python scripts/validate_gates.py
"""
from __future__ import annotations

import sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, r"C:\AI-Investo")

import pandas as pd
from engine.storage import db
from engine.backtest import harness
from engine.scoring import gates as gate_engine

pd.set_option("display.width", 250)

CRITICAL = {"surveillance", "cash_conversion", "serial_dilution", "sustained_losses"}


def verdict_of(group: pd.DataFrame) -> str:
    if ((group.status == "FAIL") & group.gate_name.isin(CRITICAL)).any():
        return "REJECTED"
    if (group.status == "FAIL").any():
        return "FLAGGED"
    if ((group.status == "UNKNOWN") & group.gate_name.isin(CRITICAL)).any():
        return "UNVETTED"
    return "CLEARED"


def main() -> None:
    con = db.connect_for_reading()
    dates = harness.rebalance_dates(con, 12, include_non_pit=False)
    print("rebalance dates:", [str(d) for d in dates], flush=True)

    per_verdict, per_gate = [], []
    for as_of in dates:
        frame = gate_engine.run_gates(con, as_of=as_of, include_non_pit=False,
                                      persist=False)
        if frame.empty:
            continue
        verdicts = (frame.groupby("security_id", group_keys=False)
                    .apply(verdict_of).rename("verdict").reset_index())
        returns = harness.forward_return(con, verdicts.security_id.tolist(), as_of, 12)
        merged = verdicts.merge(returns, on="security_id", how="inner")
        if len(merged) < 100:
            print(f"  {as_of}: only {len(merged)} names, skipped", flush=True)
            continue

        merged["as_of"] = as_of
        per_verdict.append(merged)

        # Per gate: did FAILING this gate precede worse returns than passing it?
        wide = frame.merge(returns, on="security_id", how="inner")
        for name, part in wide.groupby("gate_name"):
            fail = part.loc[part.status == "FAIL", "fwd_return"]
            ok = part.loc[part.status == "PASS", "fwd_return"]
            if len(fail) >= 5 and len(ok) >= 5:
                per_gate.append({
                    "as_of": as_of, "gate": name,
                    "n_fail": len(fail), "n_pass": len(ok),
                    "fail_ret": fail.mean(), "pass_ret": ok.mean(),
                    "gap": fail.mean() - ok.mean(),
                })
        print(f"  {as_of}: {len(merged)} names, "
              f"{merged.verdict.value_counts().to_dict()}", flush=True)

    if not per_verdict:
        print("no usable periods")
        return

    all_v = pd.concat(per_verdict, ignore_index=True)

    print("\n=== FORWARD RETURN BY VERDICT (pooled, all dates) ===")
    pooled = (all_v.groupby("verdict")["fwd_return"]
              .agg(["count", "mean", "median"]).round(1)
              .reindex(["REJECTED", "FLAGGED", "UNVETTED", "CLEARED"]))
    print(pooled.to_string())
    print(f"\nuniverse mean {all_v.fwd_return.mean():.1f}%")

    print("\n=== BY VERDICT, PER DATE (mean %) ===")
    per_date = (all_v.pivot_table(index="as_of", columns="verdict",
                                  values="fwd_return", aggfunc="mean").round(1))
    print(per_date.to_string())

    print("\n=== CLEARED minus REJECTED, per date ===")
    if {"CLEARED", "REJECTED"} <= set(per_date.columns):
        gap = (per_date["CLEARED"] - per_date["REJECTED"]).round(1)
        print(gap.to_string())
        print(f"\nmean gap {gap.mean():+.1f} pp, positive in "
              f"{int((gap > 0).sum())} of {len(gap)} periods")

    if per_gate:
        print("\n=== PER GATE: mean forward return when FAILED vs PASSED ===")
        g = pd.DataFrame(per_gate)
        summary = (g.groupby("gate")
                   .agg(periods=("gap", "size"), avg_n_fail=("n_fail", "mean"),
                        fail_ret=("fail_ret", "mean"), pass_ret=("pass_ret", "mean"),
                        gap=("gap", "mean"))
                   .sort_values("gap").round(1))
        print(summary.to_string())
        print("\nNegative gap = failing the gate preceded WORSE returns, i.e. the "
              "gate was right to fire.")

    con.close()


if __name__ == "__main__":
    main()
