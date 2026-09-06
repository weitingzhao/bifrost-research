"""Similar-regime summary — what happened after readings like this one.

Pure: takes the neighbour rows the k-NN returned and reduces them to the numbers
a verdict can quote. Only resolved neighbours (a forward return exists) count;
an unsettled neighbour is a date, not evidence.
"""

from __future__ import annotations

from statistics import median
from typing import Any


def _quantile(sorted_vals: list[float], q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def summarize_forward_returns(rows: list[dict[str, Any]], *, horizon: int) -> dict[str, Any]:
    resolved = sorted(float(r["fwd_return"]) for r in rows if r.get("fwd_return") is not None)
    out: dict[str, Any] = {
        "horizon": horizon,
        "n": len(rows),
        "n_resolved": len(resolved),
        "median_fwd": None,
        "p25_fwd": None,
        "p75_fwd": None,
        "share_positive": None,
    }
    if not resolved:
        return out
    out["median_fwd"] = round(median(resolved), 6)
    out["p25_fwd"] = round(_quantile(resolved, 0.25), 6)
    out["p75_fwd"] = round(_quantile(resolved, 0.75), 6)
    out["share_positive"] = round(sum(1 for v in resolved if v > 0) / len(resolved), 4)
    return out
