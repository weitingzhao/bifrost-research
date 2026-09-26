"""Minimal NYSE trading-day helpers (no Plugin dependency)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


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

    - ``raw_market.us_market_holiday`` (the plugin's vendor feed) lists upcoming
      holidays only — it covers today and later; ``early-close`` days trade;
    - before today, a weekday on which none of SPY, QQQ and IWM has a daily bar
      was not a session (2026-07-01…09-25 on DEV: exactly 07-03 and 09-07).
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
