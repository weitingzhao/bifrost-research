"""SQL layer for Vol Surface (SVI) tables — Wave RS-B-Surface2.

Reads ``features.option_surface_fit_daily`` and
``features.option_surface_residual_daily``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence

from bifrost_research.lenses.slope_tenor import SLOPE_PICK_ORDER, slope_window_sql


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
            WHERE symbol = %s
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
        WHERE symbol = %s AND trade_date = %s
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
        WHERE symbol = %s AND trade_date = %s
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
        WHERE symbol = %s
          AND trade_date = %s
          AND expiry = %s
        ORDER BY strike
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sym, td, expiry))
        rows = cur.fetchall() or []
    return [_row_to_dict(r, _RESIDUAL_COLUMNS) for r in rows]


# The ~30-day read, as every skew reader takes it (lenses/slope_tenor.py).
_SKEW_WINDOW = slope_window_sql()


def get_skew_extremes(conn: Any, *, as_of: date, limit: int = 20) -> list[dict[str, Any]]:
    """Top-N symbols by |atm_slope| on the ``as_of`` session only.

    A name whose freshest ~30-day fit predates ``as_of`` is not ranked here —
    see :func:`get_skew_left_out`.
    """
    lim = max(1, min(int(limit), 200))
    sql = f"""
        WITH session_fits AS (
            SELECT DISTINCT ON (symbol) {_cols(_FIT_COLUMNS)}
            FROM features.option_surface_fit_daily
            WHERE trade_date = %s
              AND {_SKEW_WINDOW}
            ORDER BY symbol, {SLOPE_PICK_ORDER}
        )
        SELECT * FROM session_fits
        ORDER BY ABS(atm_slope) DESC, symbol
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (as_of, lim))
        rows = cur.fetchall() or []
    return [_row_to_dict(r, _FIT_COLUMNS) for r in rows]


def count_skew_names(conn: Any, *, as_of: date) -> int:
    """How many names have a ~30-day fit on the ``as_of`` session."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT symbol)
            FROM features.option_surface_fit_daily
            WHERE trade_date = %s
              AND {_SKEW_WINDOW}
            """,
            (as_of,),
        )
        row = cur.fetchone()
    if row is None:
        return 0
    v = row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
    return int(v or 0)


def get_skew_left_out(conn: Any, *, as_of: date) -> list[dict[str, Any]]:
    """Names with a ~30-day fit before ``as_of`` but none on it, newest first.

    ``reason``: ``not_fit`` — no surface fit for the name that session at all;
    ``no_30d_fit`` — fit that session, but no expiry in 20–45 DTE fit with a slope.
    """
    sql = f"""
        WITH last_reading AS (
            SELECT DISTINCT ON (symbol) symbol, trade_date
            FROM features.option_surface_fit_daily
            WHERE {_SKEW_WINDOW}
            ORDER BY symbol ASC, trade_date DESC
        )
        SELECT
            l.symbol,
            l.trade_date,
            EXISTS (
                SELECT 1 FROM features.option_surface_fit_daily AS f
                WHERE f.symbol = l.symbol AND f.trade_date = %s
            ) AS fit_on_as_of
        FROM last_reading AS l
        WHERE l.trade_date < %s
        ORDER BY l.trade_date DESC, l.symbol ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (as_of, as_of))
        rows = cur.fetchall() or []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _row_to_dict(r, ("symbol", "trade_date", "fit_on_as_of"))
        out.append(
            {
                "symbol": d.get("symbol"),
                "trade_date": d.get("trade_date"),
                "reason": "no_30d_fit" if d.get("fit_on_as_of") else "not_fit",
            }
        )
    return out


def skew_extremes_payload(conn: Any, *, limit: int = 20) -> dict[str, Any]:
    """The skew-extremes read: the latest fit session ranked, and who it left out."""
    as_of = latest_trade_date(conn)
    session = date.fromisoformat(as_of) if as_of else None
    rows = get_skew_extremes(conn, as_of=session, limit=limit) if session else []
    left_out = get_skew_left_out(conn, as_of=session) if session else []
    return {
        "rows": rows,
        "count": len(rows),
        "limit": limit,
        "as_of": as_of,
        "ranked": count_skew_names(conn, as_of=session) if session else 0,
        "excluded": left_out,
        "excluded_count": len(left_out),
    }


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
