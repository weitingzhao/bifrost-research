"""Minimal NYSE trading-day helpers (no Plugin dependency)."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    if not s:
        return None
    return date.fromisoformat(s)


def fetch_closed_holiday_dates(
    conn: Any,
    *,
    start: date,
    end: date,
) -> set[date]:
    """Weekdays between ``start`` and ``end`` on which NYSE did not trade.

    Until 2026-09-26 this read ``raw_market.trading_calendar``, a table that does
    not exist: the query failed, the set came back empty, and every weekday
    counted as a session — forecast wrote sessions for Labor Day (2026-09-07)
    and settlement paired Friday 09-04's session with the holiday instead of
    09-08. Two sources now, because neither covers both directions:

    - ``raw_market.us_market_holiday`` (the plugin's vendor feed) for today and
      later; ``early-close`` days trade. It also holds 2020 on (loaded once,
      2026-05-04), but early closes only as they come up — corrected here
      2026-09-26, the note had said it lists upcoming holidays only;
    - before today, a weekday on which none of SPY, QQQ and IWM has a daily bar
      was not a session (2026-07-01…09-25 on DEV: exactly 07-03 and 09-07), which
      reads what actually traded rather than what was announced.
    """
    closed: set[date] = set()
    today = datetime.now(_NY).date()

    def _rows(sql: str, params: tuple[Any, ...]) -> list[Any]:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall() or []) if hasattr(cur, "fetchall") else []
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return []

    def _first_date(row: Any) -> date | None:
        if isinstance(row, Mapping):
            return _as_date(next(iter(row.values()), None))
        return _as_date(row[0] if row else None)

    for row in _rows(
        """
        SELECT DISTINCT holiday_date
        FROM raw_market.us_market_holiday
        WHERE status = 'closed' AND holiday_date >= %s AND holiday_date <= %s
        """,
        (start, end),
    ):
        d = _first_date(row)
        if d is not None:
            closed.add(d)

    past_end = min(end, today - timedelta(days=1))
    if start <= past_end:
        traded = {
            d
            for d in (
                _first_date(r)
                for r in _rows(
                    """
                    SELECT DISTINCT bar_date
                    FROM raw_market.stock_daily
                    WHERE symbol IN ('SPY', 'QQQ', 'IWM')
                      AND bar_date >= %s AND bar_date <= %s
                    """,
                    (start, past_end),
                )
            )
            if d is not None
        }
        # No bars at all for the window means the source is unreadable, not a
        # month of holidays — mark nothing rather than close every day.
        if traded:
            day = start
            while day <= past_end:
                if day.weekday() < 5 and day not in traded:
                    closed.add(day)
                day += timedelta(days=1)
    return closed


_CLOSED_CACHE: dict[tuple[date, date], tuple[float, frozenset[date]]] = {}
_CLOSED_TTL_S = 3600.0


def cached_closed_days(conn: Any, start: date, end: date) -> frozenset[date]:
    """``fetch_closed_holiday_dates`` behind a one-hour cache keyed on the window.

    An unreadable calendar counts weekends only (an empty set, not cached), so
    a holiday then costs one session of lead and never fails the caller. Shared
    by draft expiry (0.166.0) and hypothesis settlement (0.168.0).
    """
    hit = _CLOSED_CACHE.get((start, end))
    if hit is not None and time.monotonic() - hit[0] < _CLOSED_TTL_S:
        return hit[1]
    try:
        days = frozenset(d for d in fetch_closed_holiday_dates(conn, start=start, end=end) if isinstance(d, date))
    except Exception as exc:  # noqa: BLE001
        logger.warning("trading calendar unreadable, weekends only: %s", str(exc)[:160])
        return frozenset()
    if days:
        _CLOSED_CACHE[(start, end)] = (time.monotonic(), days)
    return days


def is_session(d: date, closed: AbstractSet[date]) -> bool:
    """A weekday NYSE did not close (``closed`` from ``fetch_closed_holiday_dates``)."""
    return d.weekday() < 5 and d not in closed


def nth_session_after(d: date, n: int, closed: AbstractSet[date]) -> date:
    """The ``n``-th trading day strictly after ``d`` (``n = 0`` returns ``d``)."""
    cur = d
    seen = 0
    while seen < n:
        cur += timedelta(days=1)
        if is_session(cur, closed):
            seen += 1
    return cur


def first_session_on_or_after(d: date, closed: AbstractSet[date]) -> date:
    """``d`` itself when it traded, else the next trading day."""
    cur = d
    while not is_session(cur, closed):
        cur += timedelta(days=1)
    return cur


def settlement_session(trade_date: date, horizon: int, closed: AbstractSet[date]) -> date:
    """The session whose close settles a ``horizon``-session forward window from ``trade_date``.

    The calendar form of ``engines/candidate_outcome/entry.py::_forward_leg``:
    entry is the first bar on or after ``trade_date`` (a Sunday candidate enters
    on Monday), exit is ``horizon`` bars after entry. The engine counts the
    symbol's own bars, so a halted name can settle later than this date; a
    settled ``candidate_outcome.exit_date`` always wins over the projection.
    """
    return nth_session_after(first_session_on_or_after(trade_date, closed), horizon, closed)


#: NYSE's regular close in New York time. On an early-close day (13:00) the
#: answer stays on the previous session until 16:00, which is the safe side.
NY_REGULAR_CLOSE = dt_time(16, 0)


def latest_closed_session(conn: Any, *, now: datetime | None = None) -> date:
    """The newest NYSE session whose regular close has passed, by the New York clock.

    The session a nightly batch describes. ``current_date`` in the database is
    UTC, and research_trading_day fires at 22:30 New York = 02:30 UTC the next
    day, so the UTC date is one calendar day late every night (TD-87: SEPA was
    stamped Tuesday for Monday and Saturday for Friday). Holidays come from
    ``cached_closed_days``; an unreadable calendar counts weekends only.
    """
    ny = (now or datetime.now(timezone.utc)).astimezone(_NY)
    day = ny.date() if ny.time() >= NY_REGULAR_CLOSE else ny.date() - timedelta(days=1)
    closed = cached_closed_days(conn, day - timedelta(days=21), day)
    while not is_session(day, closed):
        day -= timedelta(days=1)
    return day


def fetch_recent_trading_days(
    conn: Any,
    n: int,
    *,
    as_of: date | None = None,
) -> list[date]:
    """Return up to ``n`` most recent NYSE trading days ending at ``as_of``."""
    end = as_of or datetime.now(timezone.utc).date()
    lookback_start = end - timedelta(days=max(n * 4, n + 60))
    closed = fetch_closed_holiday_dates(conn, start=lookback_start, end=end)
    out: list[date] = []
    cur_d = end
    guard = 0
    max_steps = max(n * 4, n + 60)
    while len(out) < n and guard < max_steps:
        if cur_d.weekday() < 5 and cur_d not in closed:
            out.append(cur_d)
        cur_d -= timedelta(days=1)
        guard += 1
    return sorted(out)


def load_symbols_from_env_or_query(
    conn: Any,
    *,
    symbols: Sequence[str] | None = None,
) -> list[str]:
    """Resolve underlyings: explicit list, RESEARCH_WATCHLIST env, the universe rule, or distinct OI underlyings.

    `research.option_universe` is the rule (blueprint C-F5); the open-interest
    query behind it is "whatever the Plugin ingested" and stays only as the
    fallback for a database where the table is empty or absent.
    """
    if symbols:
        return sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    env = (os_environ_watchlist())
    if env:
        return env
    ruled = load_symbols_from_universe_rule(conn)
    if ruled:
        return ruled
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT underlying
                FROM raw_market.option_open_interest
                WHERE trade_date >= CURRENT_DATE - INTERVAL '5 days'
                ORDER BY 1
                LIMIT 5000
                """
            )
            rows = cur.fetchall() if hasattr(cur, "fetchall") else []
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []
    out: list[str] = []
    for row in rows or []:
        if isinstance(row, Mapping):
            sym = row.get("underlying") or next(iter(row.values()), None)
        else:
            sym = row[0] if row else None
        if sym:
            out.append(str(sym).strip().upper())
    return sorted(set(out))


def load_symbols_from_universe_rule(conn: Any) -> list[str]:
    """Every symbol in `research.option_universe`, or [] when it is empty or missing."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM research.option_universe ORDER BY 1")
            rows = cur.fetchall() if hasattr(cur, "fetchall") else []
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []
    out: list[str] = []
    for row in rows or []:
        sym = row.get("symbol") if isinstance(row, Mapping) else (row[0] if row else None)
        if sym:
            out.append(str(sym).strip().upper())
    return sorted(set(out))


def os_environ_watchlist() -> list[str]:
    import os

    raw = (os.environ.get("RESEARCH_WATCHLIST") or "").strip()
    if not raw:
        return []
    return sorted({s.strip().upper() for s in raw.split(",") if s.strip()})


DEFAULT_IV_RADAR_BENCHMARKS = ("SPY", "QQQ", "IWM")


def union_iv_radar_benchmarks(symbols: Sequence[str]) -> list[str]:
    return sorted({*symbols, *DEFAULT_IV_RADAR_BENCHMARKS})
