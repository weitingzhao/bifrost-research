"""Guards on GEX levels: what not to write (TD-136, TD-157, TD-166).

``compute_gex_levels`` picks the call wall as max(call gex) and the put wall as
min(put gex), and falls back to the strike nearest spot for zero gamma. Where a
side, or the whole distribution, carries no gamma exposure those picks are just
the first strike listed.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def has_gamma_exposure(levels: Mapping[str, Any]) -> bool:
    """Whether a strike distribution carries any gamma exposure at all.

    Without it both walls are just the first strike listed and zero gamma is the
    strike nearest spot: an expiry whose open interest is all zero (CTVA's new
    series on 2026-10-02, the day after its spin-off), or whose few contracts sit
    so far from spot that their gamma rounds to nothing (GOOG 2027-12-17 with only
    the 75 strike at spot 357). 1,534 such levels rows stood on 2026-10-06 (TD-136).
    """
    return bool(float(levels.get("call_wall_gex") or 0) or float(levels.get("put_wall_gex") or 0))


def drop_empty_side_walls(levels: Mapping[str, Any]) -> dict[str, Any]:
    """The levels with a wall removed where its side carries no gamma exposure.

    The walls are picked independently (max call gex, min put gex), so when only
    one side has exposure the other side's "wall" is the first strike listed with
    wall gex 0. 1,762 such rows on 244 names stood on 2026-10-06 (TD-157); NULL
    reads as missing, which terrain, the tier mart and the pages already handle.
    Daily levels only: the intraday snapshot sums every expiry, and its timeline
    chart draws a missing wall at zero.
    """
    out = dict(levels)
    if not float(out.get("call_wall_gex") or 0):
        out["major_call_wall"] = None
        out["call_wall_gex"] = None
    if not float(out.get("put_wall_gex") or 0):
        out["major_put_wall"] = None
        out["put_wall_gex"] = None
    return out


def zero_gamma_crossing(sorted_rows: Sequence[Mapping[str, Any]], spot: float) -> float | None:
    """The zero crossing of cumulative net gex nearest spot, or None without one.

    A crossing is a change of sign between non-zero cumulative values; the level
    is interpolated between the two strikes, or is the first strike where the
    cumulative sits at zero in between. Until 0.192.0 leaving zero counted as a
    crossing too: an expiry whose low strikes carry no gamma "flipped" at the
    first strike that did (5,408 levels rows on 2026-10-06, TD-166). ``sorted_rows``
    are the distribution rows in strike order.
    """
    cum = 0.0
    last_strike: float | None = None
    last_cum = 0.0
    zero_at: float | None = None
    best: float | None = None
    best_dist = float("inf")
    for r in sorted_rows:
        sk = float(r["strike"])
        cum += float(r.get("net_gex") or 0)
        if cum == 0:
            if last_strike is not None and zero_at is None:
                zero_at = sk
            continue
        if last_strike is not None and (last_cum > 0) != (cum > 0):
            zg = zero_at if zero_at is not None else last_strike + (-last_cum / (cum - last_cum)) * (sk - last_strike)
            if abs(zg - spot) < best_dist:
                best_dist = abs(zg - spot)
                best = round(zg, 4)
        last_strike, last_cum, zero_at = sk, cum, None
    return best


def drop_fallback_zero_gamma(levels: Mapping[str, Any]) -> dict[str, Any]:
    """The levels without a zero gamma where cumulative gamma never changes sign.

    ``compute_gex_levels`` then falls back to the strike nearest spot, which terrain
    read as a flip right at spot. 26,518 of 69,440 daily levels rows (38%) had no
    crossing on 2026-10-06 (TD-166); NULL reads as missing everywhere downstream.
    Daily levels only, as for the walls.
    """
    out = dict(levels)
    if out.get("zero_gamma_source") != "flip":
        out["zero_gamma"] = None
    return out
