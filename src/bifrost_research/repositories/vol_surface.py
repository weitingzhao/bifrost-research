"""SQL layer for Vol Surface (SVI) tables — Wave RS-B-Surface2.

Reads ``features.option_surface_fit_daily`` and
``features.option_surface_residual_daily``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence


_FIT_COLUMNS: tuple[str, ...] = (
    "symbol",
    "trade_date",
    "expiry",
    "dte",
    "svi_a",
    "svi_b",
    "svi_rho",
    "svi_m",
    "svi_sigma",
    "atm_vol",
    "atm_slope",
    "fit_rmse",
    "n_points",
    "computed_at",
)

_RESIDUAL_COLUMNS: tuple[str, ...] = (
    "symbol",
    "trade_date",
    "expiry",
    "strike",
    "log_moneyness",
    "iv_market",
    "iv_fitted",
    "residual",
    "residual_z",
    "computed_at",
)


def _row_to_dict(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = {col: row[col] for col in columns if col in row}
    else:
        out = {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
    for date_col in ("trade_date", "expiry"):
        v = out.get(date_col)
        if isinstance(v, datetime):
            out[date_col] = v.date().isoformat()
        elif isinstance(v, date):
            out[date_col] = v.isoformat()
    ca = out.get("computed_at")
    if isinstance(ca, datetime):
        out["computed_at"] = ca.isoformat()
    return out


def _cols(cols: Sequence[str]) -> str:
    return ", ".join(cols)


def _resolve_trade_date(
    conn: Any,
    symbol: str,
    trade_date: date | None,
) -> date | None:
    if trade_date is not None:
        return trade_date
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(trade_date)
            FROM features.option_surface_fit_daily
            WHERE UPPER(TRIM(symbol)) = %s
            """,
            (symbol.strip().upper(),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    v = row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def get_fit(
    conn: Any,
    symbol: str,
    *,
    trade_date: date | None = None,
) -> list[dict[str, Any]]:
    """All expiries fit for ``symbol`` on ``trade_date`` (or latest)."""
    sym = symbol.strip().upper()
    td = _resolve_trade_date(conn, sym, trade_date)
    if td is None:
        return []
    sql = f"""
        SELECT {_cols(_FIT_COLUMNS)}
        FROM features.option_surface_fit_daily
        WHERE UPPER(TRIM(symbol)) = %s AND trade_date = %s
        ORDER BY expiry
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sym, td))
        rows = cur.fetchall() or []
    return [_row_to_dict(r, _FIT_COLUMNS) for r in rows]


def get_term_structure(
    conn: Any,
    symbol: str,
    *,
    trade_date: date | None = None,
) -> list[dict[str, Any]]:
    """ATM vol vs DTE curve (single trade_date, all expiries)."""
    sym = symbol.strip().upper()
    td = _resolve_trade_date(conn, sym, trade_date)
    if td is None:
        return []
    sql = """
        SELECT expiry, dte, atm_vol, atm_slope, fit_rmse, n_points
        FROM features.option_surface_fit_daily
        WHERE UPPER(TRIM(symbol)) = %s AND trade_date = %s
          AND atm_vol IS NOT NULL
        ORDER BY dte
    """
    cols = ("expiry", "dte", "atm_vol", "atm_slope", "fit_rmse", "n_points")
    with conn.cursor() as cur:
        cur.execute(sql, (sym, td))
        rows = cur.fetchall() or []
    return [_row_to_dict(r, cols) for r in rows]


def get_residuals(
    conn: Any,
    symbol: str,
    expiry: date,
    *,
    trade_date: date | None = None,
) -> list[dict[str, Any]]:
    sym = symbol.strip().upper()
    td = _resolve_trade_date(conn, sym, trade_date)
    if td is None:
        return []
    sql = f"""
        SELECT {_cols(_RESIDUAL_COLUMNS)}
        FROM features.option_surface_residual_daily
        WHERE UPPER(TRIM(symbol)) = %s
          AND trade_date = %s
          AND expiry = %s
        ORDER BY strike
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sym, td, expiry))
        rows = cur.fetchall() or []
    return [_row_to_dict(r, _RESIDUAL_COLUMNS) for r in rows]


def get_skew_extremes(conn: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """Top-N symbols with the most extreme ATM skew (|atm_slope|)."""
    lim = max(1, min(int(limit), 200))
    sql = """
        WITH latest_per_symbol AS (
            SELECT DISTINCT ON (symbol) symbol, trade_date, expiry, dte,
                   svi_a, svi_b, svi_rho, svi_m, svi_sigma,
                   atm_vol, atm_slope, fit_rmse, n_points, computed_at
            FROM features.option_surface_fit_daily
            WHERE atm_slope IS NOT NULL
              AND dte BETWEEN 20 AND 45
            ORDER BY symbol, trade_date DESC, ABS(dte - 30) ASC
        )
        SELECT * FROM latest_per_symbol
        ORDER BY ABS(atm_slope) DESC NULLS LAST
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (lim,))
        rows = cur.fetchall() or []
    return [_row_to_dict(r, _FIT_COLUMNS) for r in rows]


def latest_trade_date(conn: Any) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) FROM features.option_surface_fit_daily")
        row = cur.fetchone()
    if row is None:
        return None
    v = row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]
