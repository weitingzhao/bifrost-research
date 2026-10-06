"""The risk-free rate for research pricing — one Treasury reader (TD-110).

Until 0.189.0 the backtester read ``raw_market.treasury_yield`` twice, in
``backtest/event_query._risk_free_rate`` and ``backtest/sim/chain``, and the IV
features read it not at all (r = 0). Both now come from here: the 1-month
constant-maturity yield (3-month when the 1-month is missing) on or before the
date, as a decimal, no older than ``MAX_STALENESS_DAYS``.

An unknown rate is 0.0 and says so in the log; a value outside [0, 25%) is
treated as unknown (a decimal feed would read 0.04, a percent feed 4.04 — both
are accepted).
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: A yield older than this does not stand for the date (a holiday week is ~5 days).
MAX_STALENESS_DAYS = 14

_SQL = """
    SELECT yield_date, COALESCE(yield_1_month, yield_3_month)
    FROM raw_market.treasury_yield
    WHERE yield_date BETWEEN %s AND %s
      AND COALESCE(yield_1_month, yield_3_month) IS NOT NULL
    ORDER BY yield_date
"""


def _as_decimal(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    v = v / 100.0 if v > 1.0 else v
    return v if 0.0 <= v < 0.25 else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _col(row: Any, i: int) -> Any:
    if isinstance(row, Mapping):
        return list(row.values())[i]
    return row[i]


class RateCurve:
    """Treasury rates by date; ``on_or_before`` answers with the newest one not too stale."""

    def __init__(self, points: Mapping[date, float]) -> None:
        self._days = sorted(points)
        self._rates = dict(points)

    def __len__(self) -> int:
        return len(self._days)

    def on_or_before(self, d: date) -> float:
        i = bisect.bisect_right(self._days, d) - 1
        if i < 0 or (d - self._days[i]).days > MAX_STALENESS_DAYS:
            return 0.0
        return self._rates[self._days[i]]


def load_rate_curve(conn: Any, start: date, end: date) -> RateCurve:
    """Every usable yield in ``[start - MAX_STALENESS_DAYS, end]``.

    A failed read rolls back, logs a warning and returns an empty curve (every
    date then prices at 0.0), as both backtest readers did before.
    """
    points: dict[date, float] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL, (start - timedelta(days=MAX_STALENESS_DAYS), end))
            rows = cur.fetchall() or []
    except Exception as exc:
        logger.warning("treasury yields unreadable, pricing at r=0: %s", str(exc)[:160])
        try:
            conn.rollback()
        except Exception:  # the read error is the one to report
            pass
        return RateCurve({})
    for row in rows:
        d, v = _as_date(_col(row, 0)), _as_decimal(_col(row, 1))
        if d is not None and v is not None:
            points[d] = v
    return RateCurve(points)


def risk_free_rate(conn: Any, on_or_before: date, cache: dict[date, float] | None = None) -> float:
    """The rate for one date (``load_rate_curve`` for a window), memoised in ``cache``."""
    if cache is not None and on_or_before in cache:
        return cache[on_or_before]
    rate = load_rate_curve(conn, on_or_before, on_or_before).on_or_before(on_or_before)
    if cache is not None:
        cache[on_or_before] = rate
    return rate
