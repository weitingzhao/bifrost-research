"""One name's constant-maturity ATM IV cone (2026-09-26; 7 days 2026-10-06).

Each session's ATM IV at 7, 30, 60 and 90 days, read off that session's expiries
in ``features.option_metric_atm_iv_daily``, and where today sits in the last
year of each. The Symbol face's term structure had seated the design's «1y
cone» as owed because no store keeps a per-horizon history; this store keeps
every expiry every session, so the horizons can be read back.

- 30 days is the one current IV (``iv30_from_expiries``): interpolated between
  the expiries that bracket 30 days, the nearest when one-sided, 7–90 DTE. It
  is the VRP store's ``atm_iv_30d`` read back for every session.
- 7 days is read like 30 (0.186.0, data gap R4.a): interpolated between the
  expiries that bracket 7 days, the nearest when one-sided, 1–21 DTE (three
  times the horizon, as 30 reaches to 90). The 30-day floor of 7 DTE keeps pin
  noise out of a monthly reading; at 7 days the near expiry is the reading, so
  the floor is the store's shortest expiry (1 DTE). NVDA 2025-09…2026-10: an
  expiry inside 7 DTE on 190 of 275 sessions, one at 7–21 DTE on all 275; across
  the store only 18% of name-sessions list an expiry inside 7 DTE, so for a
  monthly-only name the 7-day reading is mostly the nearest expiry past it —
  ``one_sided`` counts those sessions.
- 60 and 90 days need an expiry on each side (up to three times the horizon
  out). The plugin keeps chains to about 72 DTE at the median, so 60 days is
  bracketed on about four sessions in five and 90 days on one in eight
  (PLTR 2025-09…2026-09: 205 and 33 of 251). A horizon read on fewer than
  ``MIN_SESSIONS`` withholds its percentiles and says how many it had.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from bifrost_research.engines.volatility.atm_iv import (
    IV30_MAX_DTE,
    IV30_MIN_DTE,
    _as_date,
    _valid_iv,
    interpolate_iv_at_dte,
    iv30_from_expiries,
)

TENORS: tuple[int, ...] = (7, 30, 60, 90)
# The 7-day reading's DTE window: the store's shortest expiry to three times the horizon.
TENOR7_MIN_DTE = 1
TENOR7_MAX_DTE = 21
WINDOW_SESSIONS = 252
MIN_SESSIONS = 60
# 252 sessions is about 366 calendar days; the fetch reaches past it for holidays.
_FETCH_CALENDAR_DAYS = 400


def _dte_points(
    trade_date: date, pairs: Iterable[tuple[Any, Any]], lo: int, hi: int
) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    for expiry, iv in pairs:
        exp = _as_date(expiry)
        iv_f = _valid_iv(iv)
        if exp is None or iv_f is None:
            continue
        dte = (exp - trade_date).days
        if lo <= dte <= hi:
            points.append((dte, iv_f))
    return points


def _bracketed(points: Sequence[tuple[int, float]], tenor: int) -> bool:
    return any(d <= tenor for d, _ in points) and any(d >= tenor for d, _ in points)


def tenor_iv(trade_date: date, expiry_ivs: Iterable[tuple[Any, Any]], tenor: int) -> float | None:
    """ATM IV at ``tenor`` days on one session, or None when it cannot be read."""
    pairs = list(expiry_ivs)
    if tenor == 30:
        return iv30_from_expiries(trade_date, pairs)
    if tenor == 7:
        return interpolate_iv_at_dte(
            _dte_points(trade_date, pairs, TENOR7_MIN_DTE, TENOR7_MAX_DTE), target_dte=7
        )
    points = _dte_points(trade_date, pairs, IV30_MIN_DTE, 3 * tenor)
    if not _bracketed(points, tenor):
        return None
    return interpolate_iv_at_dte(points, target_dte=tenor)


def is_one_sided(trade_date: date, expiry_ivs: Iterable[tuple[Any, Any]], tenor: int) -> bool:
    """True when a nearest-when-one-sided tenor (7, 30) read one expiry side only."""
    lo, hi = (TENOR7_MIN_DTE, TENOR7_MAX_DTE) if tenor == 7 else (IV30_MIN_DTE, IV30_MAX_DTE)
    points = _dte_points(trade_date, expiry_ivs, lo, hi)
    return bool(points) and not _bracketed(points, tenor)


_RULES: dict[int, str] = {
    7: (
        "interpolated between the expiries bracketing 7 DTE, the nearest when one-sided "
        "(1–21 DTE) — IV30's rule at 7 days, with the floor at the shortest expiry"
    ),
    30: (
        "interpolated between the expiries bracketing 30 DTE, the nearest when one-sided "
        "(7–90 DTE) — the VRP store's IV30"
    ),
}


def _rule(tenor: int) -> str:
    return _RULES.get(tenor) or (
        f"interpolated between an expiry inside {tenor} DTE and one past it "
        f"(7–{3 * tenor} DTE); a session without both is not read"
    )


def quantile(sorted_values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated quantile of an ascending sequence."""
    n = len(sorted_values)
    if n == 0:
        return None
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return round(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo), 6)


def build_cone(
    rows: Iterable[tuple[Any, Any, Any]],
    *,
    window_sessions: int = WINDOW_SESSIONS,
    min_sessions: int = MIN_SESSIONS,
) -> dict[str, Any]:
    """Cone from ``(trade_date, expiry, atm_iv)`` rows, newest session as today."""
    by_day: dict[date, list[tuple[Any, Any]]] = {}
    for td, expiry, iv in rows:
        d = _as_date(td)
        if d is None:
            continue
        by_day.setdefault(d, []).append((expiry, iv))
    days = sorted(by_day)[-window_sessions:]
    as_of = days[-1] if days else None
    tenors: list[dict[str, Any]] = []
    for tenor in TENORS:
        series = [(d, v) for d in days if (v := tenor_iv(d, by_day[d], tenor)) is not None]
        values = sorted(v for _, v in series)
        today = series[-1][1] if series and series[-1][0] == as_of else None
        entry: dict[str, Any] = {
            "tenor_days": tenor,
            "n": len(series),
            "first": series[0][0].isoformat() if series else None,
            "last": series[-1][0].isoformat() if series else None,
            "today": today,
            "rule": _rule(tenor),
            # Sessions read off one side of the horizon (the nearest expiry stood in);
            # always 0 for 60 / 90, which need both sides.
            "one_sided": (
                sum(1 for d, _ in series if is_one_sided(d, by_day[d], tenor)) if tenor in _RULES else 0
            ),
        }
        if len(series) < min_sessions:
            entry.update(
                {k: None for k in ("p10", "p25", "p50", "p75", "p90", "min", "max", "today_pctile")}
            )
            entry["withheld"] = (
                f"read on {len(series)} of {len(days)} sessions — under {min_sessions}, "
                "so no percentile is drawn"
            )
        else:
            entry.update(
                {
                    "p10": quantile(values, 0.10),
                    "p25": quantile(values, 0.25),
                    "p50": quantile(values, 0.50),
                    "p75": quantile(values, 0.75),
                    "p90": quantile(values, 0.90),
                    "min": round(values[0], 6),
                    "max": round(values[-1], 6),
                    # The share of the window at or below today — History's definition.
                    "today_pctile": (
                        round(sum(1 for v in values if v <= today) / len(values), 4)
                        if today is not None
                        else None
                    ),
                    "withheld": None,
                }
            )
        tenors.append(entry)
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "window_sessions": window_sessions,
        "sessions_in_window": len(days),
        "min_sessions": min_sessions,
        "tenors": tenors,
    }


def fetch_cone(
    conn: Any,
    symbol: str,
    *,
    as_of: date | None = None,
    window_sessions: int = WINDOW_SESSIONS,
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    end = as_of or datetime.now(ZoneInfo("America/New_York")).date()
    start = end - timedelta(days=int(_FETCH_CALENDAR_DAYS * window_sessions / WINDOW_SESSIONS))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, expiry, atm_iv
            FROM features.option_metric_atm_iv_daily
            WHERE symbol = %s AND trade_date > %s AND trade_date <= %s
              AND atm_iv > 0
            """,
            (sym, start, end),
        )
        raw = cur.fetchall() or []
    rows = [
        (r.get("trade_date"), r.get("expiry"), r.get("atm_iv")) if isinstance(r, Mapping) else (r[0], r[1], r[2])
        for r in raw
    ]
    return {"symbol": sym, **build_cone(rows, window_sessions=window_sessions)}
