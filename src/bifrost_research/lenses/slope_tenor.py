"""The ~30-day slope every skew reader takes (Owner 2026-09-30, option A′).

One reading per name per session, from ``features.option_surface_fit_daily``:

1. **Interpolated** (``basis = 'interpolated'``) — a constant 30-day maturity,
   interpolated in T between the SVI fits either side of 30 DTE: the nearest at
   or under 30 and the nearest at or over it, both within the fit table's own
   7–90 DTE. A fit at exactly 30 DTE is both sides and is taken as it is.
2. **Window** (``basis = 'window'``) — only when a side is missing: the fit
   nearest 30 DTE within 20–45 (the 0.140.0 definition, Owner 2026-09-27
   option B), the earlier expiry on a tie.

Neither → no reading that session. Nothing is extrapolated.

``atm_slope`` is ∂w/∂k of total variance at k = 0, so one expiry's slope is not
the 30-day slope: magnitudes move with T. Interpolating in T removes that where
there are fits either side. Backtested on September's weekly names (1,691
name-days that had a fit 20–45 DTE out and fits under 20 and over 45, the shape
monthly names have — median 16 and 56 DTE): interpolating the outer two to the
in-window fit's tenor missed it by 0.57 of a typical slope with a +0.06 bias, a
daily rank correlation of 0.54 and 8.5 of the 20 steepest in common; the nearer
outer fit taken as it is missed by 0.75 with a +0.31 bias, 0.36 and 6 of 20.
Two in-window fits of one name on one day agree only to 0.33 in rank, so the
interpolation error sits inside the slope's own noise.

Why both steps. The 20–45 window alone left names that list only monthlies dark
for about a week after each monthly expiry (09-28/29: 361 names read, against
652/656 interpolated). Interpolation alone leaves them dark in expiry week
instead: the expiring monthly is under the fit table's 7-DTE floor, so nothing
sits under 30 (09-14/15: 26 names, against 230/238 in the window). Laid over the
Oct–Dec 2026 calendar a monthly-only name is dark 10 sessions under the window,
11 under interpolation, and 0 under both, because the two gaps never coincide.

The readers are skew-extremes, the scan's atm_slope_30d, the skew lens exhibit
(its reading and its 252-day history), the signal-hit skew trigger, and the
term_slope similar-regime k-NN. ``tests/lenses/test_slope_tenor.py`` holds
each of them to this module.
"""

from __future__ import annotations

#: The maturity every reader reads the slope at.
SLOPE_TARGET_DTE = 30
#: The fit table's own tenor range (``engines/vol_surface/entry.py``): a
#: bracketing fit can be anywhere in it.
SLOPE_SOURCE_DTE_MIN = 7
SLOPE_SOURCE_DTE_MAX = 90
#: The fallback window (step 2).
SLOPE_DTE_MIN = 20
SLOPE_DTE_MAX = 45

_FIT_TABLE = "features.option_surface_fit_daily"
#: Among fits inside the window: nearest 30 DTE, the earlier expiry on a tie.
_WINDOW_PICK_ORDER = f"ABS(dte - {SLOPE_TARGET_DTE}) ASC, expiry ASC"

#: The columns ``slope_30d_sql`` yields, in order.
SLOPE_30D_COLUMNS = (
    "symbol",
    "trade_date",
    "basis",
    "dte",
    "expiry",
    "atm_slope",
    "atm_vol",
    "fit_rmse",
    "n_points",
    "computed_at",
    "short_expiry",
    "short_dte",
    "long_expiry",
    "long_dte",
)

#: How many times ``slope_30d_sql`` repeats its ``where``.
SLOPE_30D_WHERE_BINDS = 3


def slope_30d_sql(where: str = "TRUE") -> str:
    """A subquery: at most one ~30-day reading per ``(symbol, trade_date)``.

    ``where`` is ANDed into each of the three scans of the fit table, so **its
    placeholders bind three times** (:data:`SLOPE_30D_WHERE_BINDS`): pass
    ``p * 3`` for a ``where`` that takes the tuple ``p``. Columns are
    :data:`SLOPE_30D_COLUMNS`:

    - ``basis`` — ``'interpolated'`` or ``'window'`` (see the module docstring).
    - ``atm_slope`` — interpolated linearly in T to 30 DTE, or the window fit's.
    - ``atm_vol`` — total variance (σ²·T) interpolated linearly in T, then back
      to a vol at 30 DTE; or the window fit's.
    - ``fit_rmse`` / ``n_points`` — for an interpolated reading the worse of the
      two fits (higher RMSE, fewer points), so a quality filter holds for both.
    - ``dte`` — 30 when interpolated, else the window fit's. ``expiry`` — the
      side nearer 30 (the shorter on a tie), else the window fit's, so a reader
      showing one expiry shows a real one. ``short_*`` / ``long_*`` name both
      inputs of an interpolated reading and are NULL on a window one.
    """
    t = SLOPE_TARGET_DTE
    fit_cols = "symbol, trade_date, expiry, dte, atm_slope, atm_vol, fit_rmse, n_points, computed_at"
    scan = f"""SELECT DISTINCT ON (symbol, trade_date) {fit_cols}
            FROM {_FIT_TABLE}
            WHERE atm_slope IS NOT NULL AND ({where})"""
    weight = f"(({t} - s.dte)::float8 / NULLIF(l.dte - s.dte, 0))"
    return f"""(
        SELECT DISTINCT ON (symbol, trade_date)
               symbol, trade_date, basis, dte, expiry, atm_slope, atm_vol, fit_rmse,
               n_points, computed_at, short_expiry, short_dte, long_expiry, long_dte
        FROM (
            SELECT 0 AS basis_rank,
                   s.symbol,
                   s.trade_date,
                   'interpolated'::text AS basis,
                   {t} AS dte,
                   CASE WHEN (l.dte - {t}) < ({t} - s.dte) THEN l.expiry ELSE s.expiry END AS expiry,
                   CASE WHEN l.dte = s.dte THEN s.atm_slope
                        ELSE s.atm_slope + (l.atm_slope - s.atm_slope) * {weight}
                   END AS atm_slope,
                   CASE WHEN l.dte = s.dte THEN s.atm_vol
                        ELSE SQRT(GREATEST(
                            s.atm_vol * s.atm_vol * s.dte
                            + (l.atm_vol * l.atm_vol * l.dte - s.atm_vol * s.atm_vol * s.dte) * {weight},
                            0
                        ) / {t})
                   END AS atm_vol,
                   GREATEST(s.fit_rmse, l.fit_rmse) AS fit_rmse,
                   LEAST(s.n_points, l.n_points) AS n_points,
                   GREATEST(s.computed_at, l.computed_at) AS computed_at,
                   s.expiry AS short_expiry,
                   s.dte AS short_dte,
                   l.expiry AS long_expiry,
                   l.dte AS long_dte
            FROM ({scan}
                  AND dte BETWEEN {SLOPE_SOURCE_DTE_MIN} AND {t}
                ORDER BY symbol, trade_date, dte DESC, expiry DESC
            ) s
            JOIN ({scan}
                  AND dte BETWEEN {t} AND {SLOPE_SOURCE_DTE_MAX}
                ORDER BY symbol, trade_date, dte ASC, expiry ASC
            ) l ON l.symbol = s.symbol AND l.trade_date = s.trade_date
            UNION ALL
            SELECT 1 AS basis_rank,
                   w.symbol,
                   w.trade_date,
                   'window'::text AS basis,
                   w.dte,
                   w.expiry,
                   w.atm_slope,
                   w.atm_vol,
                   w.fit_rmse,
                   w.n_points,
                   w.computed_at,
                   NULL::date AS short_expiry,
                   NULL::int AS short_dte,
                   NULL::date AS long_expiry,
                   NULL::int AS long_dte
            FROM ({scan}
                  AND dte BETWEEN {SLOPE_DTE_MIN} AND {SLOPE_DTE_MAX}
                ORDER BY symbol, trade_date, {_WINDOW_PICK_ORDER}
            ) w
        ) c
        ORDER BY symbol, trade_date, basis_rank
    )"""
