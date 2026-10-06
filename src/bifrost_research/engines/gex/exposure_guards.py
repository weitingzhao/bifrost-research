"""Guards on GEX levels: what not to write (TD-136, TD-157).

``compute_gex_levels`` picks the call wall as max(call gex) and the put wall as
min(put gex), and falls back to the strike nearest spot for zero gamma. Where a
side, or the whole distribution, carries no gamma exposure those picks are just
the first strike listed.
"""

from __future__ import annotations

from typing import Any, Mapping


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
