"""SQL layer for ``features.stock_signal_vrp_daily`` — Wave RS-B-VRP2.

Read-only. All rows come from the VRP engine (`engines/vrp/*`).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Protocol, Sequence


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
        WHERE UPPER(TRIM(symbol)) = %s
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
        WHERE UPPER(TRIM(symbol)) = %s
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
    bucket: str = "high",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Top-N most recent trade_date rows with extreme ``vrp_pct_252d``.

    ``bucket``:
      - ``"high"``: highest percentiles (sell-vol candidates)
      - ``"low"``:  lowest percentiles (buy-vol candidates)
    """
    if bucket not in ("high", "low"):
        raise ValueError("bucket must be 'high' or 'low'")
    order_dir = "DESC" if bucket == "high" else "ASC"
    lim = max(1, min(int(limit), 200))
    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (symbol) {_cols()}
            FROM features.stock_signal_vrp_daily
            WHERE vrp_pct_252d IS NOT NULL
            ORDER BY symbol, trade_date DESC
        )
        SELECT * FROM latest
        ORDER BY vrp_pct_252d {order_dir} NULLS LAST, symbol
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (lim,))
        rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]


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
