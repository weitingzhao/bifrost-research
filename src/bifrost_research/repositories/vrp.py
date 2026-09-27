"""SQL layer for ``features.stock_signal_vrp_daily`` — Wave RS-B-VRP2.

Read-only. All rows come from the VRP engine (`engines/vrp/*`).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Protocol, Sequence

from bifrost_research.repositories.listing_status import liveness_floor, retired_sql, split_retired


class _Connection(Protocol):
    def cursor(self) -> Any: ...


_VRP_COLUMNS: tuple[str, ...] = (
    "symbol",
    "trade_date",
    "rv_20d",
    "rv_60d",
    "rv_252d",
    "atm_iv_30d",
    "vrp_20d",
    "vrp_60d",
    "vrp_pct_252d",
    "fwd_ret_20d",
    "computed_at",
)


def _row_to_dict(row: Any, columns: Sequence[str] = _VRP_COLUMNS) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = {col: row[col] for col in columns if col in row}
    else:
        out = {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
    if isinstance(out.get("trade_date"), (date, datetime)):
        td = out["trade_date"]
        out["trade_date"] = td.date().isoformat() if isinstance(td, datetime) else td.isoformat()
    if isinstance(out.get("computed_at"), datetime):
        out["computed_at"] = out["computed_at"].isoformat()
    return out


def _cols() -> str:
    return ", ".join(_VRP_COLUMNS)


def get_latest(conn: _Connection, symbol: str) -> dict[str, Any] | None:
    """Latest VRP row for ``symbol`` (order by trade_date DESC)."""
    sym = symbol.strip().upper()
    sql = f"""
        SELECT {_cols()}
        FROM features.stock_signal_vrp_daily
        WHERE symbol = %s
        ORDER BY trade_date DESC
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sym,))
        row = cur.fetchone()
    return _row_to_dict(row) if row is not None else None


def get_history(
    conn: _Connection,
    symbol: str,
    *,
    days: int = 252,
) -> list[dict[str, Any]]:
    """Trailing ``days`` VRP rows for ``symbol`` in ascending date order."""
    sym = symbol.strip().upper()
    limit = max(1, min(int(days), 5000))
    sql = f"""
        SELECT {_cols()}
        FROM features.stock_signal_vrp_daily
        WHERE symbol = %s
        ORDER BY trade_date DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sym, limit))
        rows = cur.fetchall() or []
    ordered = [_row_to_dict(r) for r in rows]
    ordered.sort(key=lambda r: r.get("trade_date") or "")
    return ordered


def get_extremes(
    conn: _Connection,
    *,
    as_of: date,
    bucket: str = "high",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Top-N rows of the ``as_of`` session with extreme ``vrp_pct_252d``.

    ``bucket``:
      - ``"high"``: highest percentiles (sell-vol candidates)
      - ``"low"``:  lowest percentiles (buy-vol candidates)

    A name whose freshest percentile predates ``as_of`` is not ranked here —
    see :func:`get_left_out`.
    """
    if bucket not in ("high", "low"):
        raise ValueError("bucket must be 'high' or 'low'")
    order_dir = "DESC" if bucket == "high" else "ASC"
    lim = max(1, min(int(limit), 200))
    sql = f"""
        SELECT {_cols()}
        FROM features.stock_signal_vrp_daily
        WHERE trade_date = %s
          AND vrp_pct_252d IS NOT NULL
        ORDER BY vrp_pct_252d {order_dir}, symbol
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (as_of, lim))
        rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]


def count_ranked(conn: _Connection, *, as_of: date) -> int:
    """How many names carry a 252-day percentile on the ``as_of`` session."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM features.stock_signal_vrp_daily
            WHERE trade_date = %s
              AND vrp_pct_252d IS NOT NULL
            """,
            (as_of,),
        )
        row = cur.fetchone()
    if row is None:
        return 0
    v = row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
    return int(v or 0)


def get_left_out(conn: _Connection, *, as_of: date) -> list[dict[str, Any]]:
    """Names with a percentile before ``as_of`` but none on it, newest first.

    ``reason``: ``retired`` — the listing no longer trades (see
    ``repositories/listing_status.py``); ``not_computed`` — no VRP row for the name
    that session; ``no_percentile`` — a row that session, but without its 252-day
    percentile.
    """
    sql = f"""
        WITH last_reading AS (
            SELECT DISTINCT ON (symbol) symbol, trade_date
            FROM features.stock_signal_vrp_daily
            WHERE vrp_pct_252d IS NOT NULL
            ORDER BY symbol ASC, trade_date DESC
        )
        SELECT
            l.symbol,
            l.trade_date,
            EXISTS (
                SELECT 1 FROM features.stock_signal_vrp_daily AS v
                WHERE v.symbol = l.symbol AND v.trade_date = %s
            ) AS row_on_as_of,
            {retired_sql("l.symbol")} AS retired
        FROM last_reading AS l
        WHERE l.trade_date < %s
        ORDER BY l.trade_date DESC, l.symbol ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (as_of, liveness_floor(as_of), as_of))
        rows = cur.fetchall() or []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _row_to_dict(r, ("symbol", "trade_date", "row_on_as_of", "retired"))
        if d.get("retired"):
            reason = "retired"
        else:
            reason = "no_percentile" if d.get("row_on_as_of") else "not_computed"
        out.append({"symbol": d.get("symbol"), "trade_date": d.get("trade_date"), "reason": reason})
    return out


def extremes_payload(
    conn: _Connection,
    *,
    bucket: str = "high",
    limit: int = 20,
) -> dict[str, Any]:
    """The VRP-extremes read: the latest session ranked, and who it left out."""
    if bucket not in ("high", "low"):
        raise ValueError("bucket must be 'high' or 'low'")
    as_of = latest_trade_date(conn)
    session = date.fromisoformat(as_of) if as_of else None
    rows = get_extremes(conn, as_of=session, bucket=bucket, limit=limit) if session else []
    left_out, retired = split_retired(get_left_out(conn, as_of=session) if session else [])
    return {
        "rows": rows,
        "count": len(rows),
        "bucket": bucket,
        "limit": limit,
        "as_of": as_of,
        "ranked": count_ranked(conn, as_of=session) if session else 0,
        "excluded": left_out,
        "excluded_count": len(left_out),
        "retired_count": retired,
    }


def latest_trade_date(conn: _Connection) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) FROM features.stock_signal_vrp_daily")
        row = cur.fetchone()
    if row is None:
        return None
    v = row[0] if not isinstance(row, Mapping) else row.get("max")
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]
