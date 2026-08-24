"""Minimal NYSE trading-day helpers (no Plugin dependency)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


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
    """Load closed holiday dates from market.trading_calendar when available."""
    closed: set[date] = set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date
                FROM raw_market.trading_calendar
                WHERE trade_date >= %s AND trade_date <= %s
                  AND COALESCE(is_open, true) = false
                """,
                (start, end),
            )
            rows = cur.fetchall() if hasattr(cur, "fetchall") else []
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return closed
    for row in rows or []:
        if isinstance(row, Mapping):
            d = _as_date(row.get("trade_date") or next(iter(row.values()), None))
        else:
            d = _as_date(row[0] if row else None)
        if d is not None:
            closed.add(d)
    return closed


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
    """Resolve underlyings: explicit list, RESEARCH_WATCHLIST env, or distinct OI underlyings."""
    if symbols:
        return sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    env = (os_environ_watchlist())
    if env:
        return env
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


def os_environ_watchlist() -> list[str]:
    import os

    raw = (os.environ.get("RESEARCH_WATCHLIST") or "").strip()
    if not raw:
        return []
    return sorted({s.strip().upper() for s in raw.split(",") if s.strip()})


DEFAULT_IV_RADAR_BENCHMARKS = ("SPY", "QQQ", "IWM")


def union_iv_radar_benchmarks(symbols: Sequence[str]) -> list[str]:
    return sorted({*symbols, *DEFAULT_IV_RADAR_BENCHMARKS})
