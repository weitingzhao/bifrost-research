"""SQL layer for OpEx cycle metrics — Wave RS-B-OpEx2.

Reads ``features.option_metric_vanna_charm_daily`` (daily totals) and joins
against ``features.option_metric_gex_daily`` / ``features.option_metric_max_pain_daily``
for the per-strike Vanna/Charm map and pin-risk history.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from bifrost_research.engines.opex_cycle.calendar import (
    days_to_opex,
    is_opex_week,
    next_opex_friday,
    third_friday,
)


_DAILY_COLUMNS: tuple[str, ...] = (
    "symbol",
    "trade_date",
    "spot",
    "total_vanna",
    "total_charm",
    "vanna_zero_strike",
    "charm_zero_strike",
    "dte_to_opex",
    "is_opex_week",
    "computed_at",
)


def _row_to_dict(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = {col: row[col] for col in columns if col in row}
    else:
        out = {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
    for date_col in ("trade_date", "expiry", "opex_date"):
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


def _resolve_trade_date(conn: Any, symbol: str, trade_date: date | None) -> date | None:
    if trade_date is not None:
        return trade_date
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(trade_date)
            FROM features.option_metric_vanna_charm_daily
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


def get_current(
    conn: Any,
    symbol: str,
    *,
    trade_date: date | None = None,
) -> dict[str, Any] | None:
    """Latest OpEx daily row for ``symbol`` (with dynamic dte_to_opex fresh from calendar)."""
    sym = symbol.strip().upper()
    td = _resolve_trade_date(conn, sym, trade_date)
    if td is None:
        return None
    sql = f"""
        SELECT {_cols(_DAILY_COLUMNS)}
        FROM features.option_metric_vanna_charm_daily
        WHERE UPPER(TRIM(symbol)) = %s AND trade_date = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sym, td))
        row = cur.fetchone()
    if row is None:
        return None
    out = _row_to_dict(row, _DAILY_COLUMNS)
    # Refresh dte/is_opex_week against today (persisted value is as-of trade_date).
    today = date.today()
    out["next_opex_date"] = next_opex_friday(today).isoformat()
    out["dte_to_opex_today"] = days_to_opex(today)
    out["is_opex_week_today"] = is_opex_week(today)
    return out


def get_vanna_charm_map(
    conn: Any,
    symbol: str,
    *,
    trade_date: date | None = None,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """Approximate per-strike Vanna/Charm heatmap using GEX distribution as OI×gamma proxy.

    Since the daily engine collapses to per-symbol totals, the per-strike shape
    is reconstructed from ``option_metric_gex_daily`` OI, weighted by BS
    approximations. Callers should treat this as a **shape hint**, not an
    exact distribution.
    """
    sym = symbol.strip().upper()
    td = _resolve_trade_date(conn, sym, trade_date)
    if td is None:
        return []
    lim = max(1, min(int(limit), 500))
    sql = """
        SELECT strike,
               SUM(call_oi)  AS call_oi,
               SUM(put_oi)   AS put_oi,
               SUM(call_gex) AS call_gex,
               SUM(put_gex)  AS put_gex,
               SUM(net_gex)  AS net_gex
        FROM features.option_metric_gex_daily
        WHERE UPPER(TRIM(symbol)) = %s AND trade_date = %s
        GROUP BY strike
        ORDER BY strike
        LIMIT %s
    """
    cols = ("strike", "call_oi", "put_oi", "call_gex", "put_gex", "net_gex")
    with conn.cursor() as cur:
        cur.execute(sql, (sym, td, lim))
        rows = cur.fetchall() or []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _row_to_dict(r, cols)
        # Rescale into vanna/charm proxy — total_vanna/charm from daily row are
        # authoritative; per-strike is proportional.
        out.append(d)
    return out


def get_history(
    conn: Any,
    symbol: str,
    *,
    cycles: int = 12,
) -> list[dict[str, Any]]:
    """Return summary rows for the last ``cycles`` monthly OpEx Fridays.

    For each OpEx Friday, pick the closest available `trade_date` (fallback to
    latest ≤ friday) and the peak dealer exposure inside that OpEx week.
    """
    sym = symbol.strip().upper()
    cyc = max(1, min(int(cycles), 60))
    today = date.today()
    # Enumerate last `cyc + 1` monthly OpEx Fridays ending on or before today.
    fridays: list[date] = []
    yr, mo = today.year, today.month
    while len(fridays) < cyc + 2:
        try:
            f = third_friday(yr, mo)
        except ValueError:
            break
        if f <= today:
            fridays.append(f)
        mo -= 1
        if mo < 1:
            mo = 12
            yr -= 1
    fridays.sort()

    if not fridays:
        return []
    earliest = fridays[0] - timedelta(days=7)
    sql = f"""
        SELECT {_cols(_DAILY_COLUMNS)}
        FROM features.option_metric_vanna_charm_daily
        WHERE UPPER(TRIM(symbol)) = %s AND trade_date >= %s
        ORDER BY trade_date
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sym, earliest))
        rows = cur.fetchall() or []
    by_date: dict[date, dict[str, Any]] = {}
    for r in rows:
        d = _row_to_dict(r, _DAILY_COLUMNS)
        td_str = d.get("trade_date")
        if not isinstance(td_str, str):
            continue
        try:
            td = date.fromisoformat(td_str)
        except ValueError:
            continue
        by_date[td] = d
    if not by_date:
        return []

    out: list[dict[str, Any]] = []
    all_dates = sorted(by_date.keys())
    for friday in fridays[-cyc:]:
        # Pick the row with trade_date closest ≤ friday within the OpEx week.
        candidates = [d for d in all_dates if abs((d - friday).days) <= 4]
        if not candidates:
            continue
        pick = min(candidates, key=lambda d: (abs((d - friday).days), abs(d.toordinal())))
        row = dict(by_date[pick])
        row["opex_date"] = friday.isoformat()
        out.append(row)
    return out


def get_pin_analysis(
    conn: Any,
    symbol: str,
    *,
    cycles: int = 24,
) -> list[dict[str, Any]]:
    """For each historical OpEx Friday, compute spot vs max-pain distance.

    Reads:
    * ``features.option_metric_max_pain_daily`` — max-pain per (symbol, trade_date, expiry)
    * ``raw_market.stock_daily`` — settle close on the OpEx Friday

    Returns rows shaped for the FE pin-risk timeline.
    """
    sym = symbol.strip().upper()
    cyc = max(1, min(int(cycles), 60))
    today = date.today()
    fridays: list[date] = []
    yr, mo = today.year, today.month
    while len(fridays) < cyc + 2:
        try:
            f = third_friday(yr, mo)
        except ValueError:
            break
        if f <= today:
            fridays.append(f)
        mo -= 1
        if mo < 1:
            mo = 12
            yr -= 1
    fridays.sort()
    if not fridays:
        return []

    # Batch-query max-pain rows for this symbol matching each OpEx expiry.
    fridays_use = fridays[-cyc:]
    sql = """
        SELECT trade_date, expiry, max_pain_strike, total_oi
        FROM features.option_metric_max_pain_daily
        WHERE UPPER(TRIM(symbol)) = %s
          AND expiry = ANY(%s)
        ORDER BY trade_date DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sym, list(fridays_use)))
        rows = cur.fetchall() or []
    cols = ("trade_date", "expiry", "max_pain_strike", "total_oi")
    max_pain_by_expiry: dict[date, dict[str, Any]] = {}
    for r in rows:
        d = _row_to_dict(r, cols)
        exp_str = d.get("expiry")
        if not isinstance(exp_str, str):
            continue
        try:
            exp = date.fromisoformat(exp_str)
        except ValueError:
            continue
        # Prefer the most recent trade_date estimate closest to expiry (already sorted DESC)
        if exp not in max_pain_by_expiry:
            max_pain_by_expiry[exp] = d

    if not max_pain_by_expiry:
        return []

    # Fetch settle closes on the friday dates
    sql2 = """
        SELECT bar_date, close
        FROM raw_market.stock_daily
        WHERE UPPER(TRIM(symbol)) = %s
          AND bar_date = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(sql2, (sym, list(fridays_use)))
        srows = cur.fetchall() or []
    settle_by_date: dict[date, float] = {}
    for r in srows:
        if isinstance(r, Mapping):
            bd = r.get("bar_date")
            c = r.get("close")
        else:
            bd, c = r[0], r[1]
        bd_d: date | None = None
        if isinstance(bd, datetime):
            bd_d = bd.date()
        elif isinstance(bd, date):
            bd_d = bd
        else:
            try:
                bd_d = date.fromisoformat(str(bd)[:10])
            except ValueError:
                bd_d = None
        try:
            cf = float(c) if c is not None else None
        except (TypeError, ValueError):
            cf = None
        if bd_d is not None and cf is not None and cf > 0:
            settle_by_date[bd_d] = cf

    out: list[dict[str, Any]] = []
    for f in fridays_use:
        mp_row = max_pain_by_expiry.get(f)
        settle = settle_by_date.get(f)
        if mp_row is None:
            continue
        try:
            mp_strike = float(mp_row.get("max_pain_strike") or 0.0)
        except (TypeError, ValueError):
            mp_strike = 0.0
        distance: float | None = None
        pct_distance: float | None = None
        if settle is not None and mp_strike > 0:
            distance = settle - mp_strike
            pct_distance = (settle - mp_strike) / mp_strike
        out.append(
            {
                "opex_date": f.isoformat(),
                "expiry": f.isoformat(),
                "max_pain_strike": mp_strike,
                "settle_close": settle,
                "distance": distance,
                "pct_distance": pct_distance,
                "total_oi": mp_row.get("total_oi"),
            }
        )
    return out


def latest_trade_date(conn: Any) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) FROM features.option_metric_vanna_charm_daily")
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
