"""The fit every skew reader takes as a name's ~30-day slope (Owner 2026-09-27, option B).

The SVI fit nearest 30 DTE among those 20–45 DTE out, with a slope. A name
with no expiry in that window has no ~30-day reading that session. Nothing
falls back to a two-week or a two-month slope in its place.

Until then five readers took the fit nearest 30 DTE at any tenor, and one
(skew-extremes) took it only inside 20–45, so one page could rank a name on a
slope another page did not have. The unbounded pick was not a corner case. In
September 3,774 of 7,486 name-days (51.5%) took an expiry under 20 DTE, because
weekly names carried their first three expiries only. The skew lens's own-year
percentile therefore compared two-week slopes with thirty-day ones. From the
2026-09-25 session, 11 of 605 names had no fit inside the window.

The readers are skew-extremes, the scan's atm_slope_30d, the skew lens exhibit
(its reading and its 252-day history), the signal-hit skew trigger, and the
term_slope similar-regime k-NN. ``tests/lenses/test_slope_tenor.py`` holds
each of them to this module.
"""

from __future__ import annotations

SLOPE_DTE_MIN = 20
SLOPE_DTE_MAX = 45

#: Among fits inside the window: nearest 30 DTE, the earlier expiry on a tie.
SLOPE_PICK_ORDER = "ABS(dte - 30) ASC, expiry ASC"


def slope_window_sql(alias: str = "") -> str:
    """SQL predicate: the fit row has a slope and sits 20–45 DTE out."""
    col = f"{alias}." if alias else ""
    return f"{col}atm_slope IS NOT NULL AND {col}dte BETWEEN {SLOPE_DTE_MIN} AND {SLOPE_DTE_MAX}"
