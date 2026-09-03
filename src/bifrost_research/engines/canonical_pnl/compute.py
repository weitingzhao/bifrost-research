"""Canonical PnL engine: load spots/IV, simulate, dual-write features + dw_stock mart."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from bifrost_research.engines.backtest.canonical_pnl import (
    STRUCTURES,
    StructureName,
    simulate_trajectory,
)
from bifrost_research.schema.schemas import (
    SCHEMA_DW_STOCK,
    TABLE_MART_CANONICAL_PNL_DAILY,
    TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY,
)

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO {table} (
  as_of_date, entry_date, symbol, structure, params_hash, structure_params,
  entry_spot, entry_atm_iv, entry_mid, as_of_spot, as_of_atm_iv,
  mtm_value, pnl_since_entry, dte_remaining, expired, final_pnl, data_quality
) VALUES (
  %(as_of_date)s, %(entry_date)s, %(symbol)s, %(structure)s, %(params_hash)s,
  %(structure_params)s::jsonb,
  %(entry_spot)s, %(entry_atm_iv)s, %(entry_mid)s, %(as_of_spot)s, %(as_of_atm_iv)s,
  %(mtm_value)s, %(pnl_since_entry)s, %(dte_remaining)s, %(expired)s, %(final_pnl)s,
  %(data_quality)s
)
ON CONFLICT (as_of_date, entry_date, symbol, structure, params_hash) DO UPDATE SET
  structure_params = EXCLUDED.structure_params,
  entry_spot = EXCLUDED.entry_spot,
  entry_atm_iv = EXCLUDED.entry_atm_iv,
  entry_mid = EXCLUDED.entry_mid,
  as_of_spot = EXCLUDED.as_of_spot,
  as_of_atm_iv = EXCLUDED.as_of_atm_iv,
  mtm_value = EXCLUDED.mtm_value,
  pnl_since_entry = EXCLUDED.pnl_since_entry,
  dte_remaining = EXCLUDED.dte_remaining,
  expired = EXCLUDED.expired,
  final_pnl = EXCLUDED.final_pnl,
  data_quality = EXCLUDED.data_quality,
  computed_at = now()
"""


def fetch_spot_series(
    conn: Any,
    symbol: str,
    start: date,
    end: date,
) -> dict[date, float]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bar_date, close::float
            FROM raw_market.stock_daily
            WHERE symbol = %s AND bar_date BETWEEN %s AND %s
              AND close IS NOT NULL AND close > 0
            ORDER BY bar_date
            """,
            (symbol.upper(), start, end),
        )
        return {r[0]: float(r[1]) for r in cur.fetchall()}


def _normalize_iv(raw: float) -> float:
    """Vendor IV may be percent (e.g. 18.5) or decimal (0.185)."""
    v = float(raw)
    return v / 100.0 if v > 3.0 else v


def locf_fill_iv(
    observed: Mapping[date, float],
    calendar: Sequence[date],
    *,
    max_gap_days: int = 14,
) -> dict[date, float]:
    """Last-observation-carried-forward onto ``calendar`` (sparse reconstructed IV)."""
    if not calendar:
        return {}
    out: dict[date, float] = {}
    last_iv: float | None = None
    last_d: date | None = None
    for d in sorted(calendar):
        if d in observed:
            last_iv = float(observed[d])
            last_d = d
            out[d] = last_iv
            continue
        if last_iv is None or last_d is None:
            continue
        if (d - last_d).days <= max_gap_days:
            out[d] = last_iv
    return out


def fetch_atm_iv_series(
    conn: Any,
    symbol: str,
    start: date,
    end: date,
) -> dict[date, float]:
    """Prefer ~30 DTE ATM IV from features; fall back to VRP atm_iv_30d."""
    out: dict[date, float] = {}
    with conn.cursor() as cur:
        try:
            # One IV per trade_date: expiry nearest 30 calendar days.
            cur.execute(
                """
                SELECT DISTINCT ON (trade_date)
                  trade_date, atm_iv::float
                FROM features.option_metric_atm_iv_daily
                WHERE symbol = %s AND trade_date BETWEEN %s AND %s
                  AND atm_iv IS NOT NULL AND atm_iv > 0
                  AND expiry IS NOT NULL
                ORDER BY trade_date,
                  ABS((expiry - trade_date) - 30) ASC,
                  expiry ASC
                """,
                (symbol.upper(), start, end),
            )
            for d, iv in cur.fetchall():
                out[d] = _normalize_iv(float(iv))
        except Exception:
            conn.rollback()
        if not out:
            try:
                cur.execute(
                    """
                    SELECT trade_date, atm_iv_30d::float
                    FROM features.stock_signal_vrp_daily
                    WHERE symbol = %s AND trade_date BETWEEN %s AND %s
                      AND atm_iv_30d IS NOT NULL AND atm_iv_30d > 0
                    """,
                    (symbol.upper(), start, end),
                )
                for d, iv in cur.fetchall():
                    out[d] = _normalize_iv(float(iv))
            except Exception:
                conn.rollback()
    return out


def clear_canonical_pnl_tables(conn: Any) -> None:
    """Full rebuild helper — truncate features write authority before cohort rerun."""
    with conn.cursor() as cur:
        try:
            cur.execute(f"TRUNCATE TABLE {TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY}")
        except Exception:
            conn.rollback()
            cur.execute(f"DELETE FROM {TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY}")
    conn.commit()


def upsert_marks(conn: Any, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    payloads = []
    for r in rows:
        p = dict(r)
        sp = p.get("structure_params")
        if isinstance(sp, dict):
            p["structure_params"] = json.dumps(sp)
        payloads.append(p)
    sql = _UPSERT_SQL.format(table=TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY)
    with conn.cursor() as cur:
        for p in payloads:
            cur.execute(sql, p)
    conn.commit()
    return len(payloads)


def coverage_report(conn: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT data_quality, COUNT(*)::bigint
            FROM {TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY}
            GROUP BY 1
            """
        )
        by_q = {str(r[0]): int(r[1]) for r in cur.fetchall()}
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT symbol)::bigint,
                   COUNT(DISTINCT entry_date)::bigint,
                   COUNT(*)::bigint
            FROM {TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY}
            """
        )
        row = cur.fetchone() or (0, 0, 0)
    total = int(row[2] or 0)
    insuff = int(by_q.get("insufficient_chain") or 0)
    return {
        "symbols": int(row[0] or 0),
        "entry_dates": int(row[1] or 0),
        "rows": total,
        "by_quality": by_q,
        "insufficient_pct": (insuff / total) if total else None,
        "mart_table": TABLE_MART_CANONICAL_PNL_DAILY,
        "features_table": TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY,
        "dw_schema": SCHEMA_DW_STOCK,
    }


def run_symbol_window(
    conn: Any,
    *,
    symbol: str,
    entry_dates: Sequence[date],
    as_of_end: date,
    structures: Sequence[StructureName] = STRUCTURES,
    dry_run: bool = False,
    iv_max_gap_days: int = 14,
) -> dict[str, Any]:
    if not entry_dates:
        return {"symbol": symbol, "rows_written": 0, "skipped": True}
    start = min(entry_dates)
    spots = fetch_spot_series(conn, symbol, start, as_of_end)
    observed_ivs = fetch_atm_iv_series(conn, symbol, start, as_of_end)
    # Carry sparse reconstructed / vendor IV onto the spot calendar (IDS-4).
    ivs = locf_fill_iv(observed_ivs, sorted(spots.keys()), max_gap_days=iv_max_gap_days)
    all_rows: list[dict[str, Any]] = []
    skipped_entries = 0
    for entry in entry_dates:
        if entry not in ivs or entry not in spots:
            skipped_entries += 1
            continue
        mark_end = min(as_of_end, entry + timedelta(days=60))
        as_ofs = sorted(d for d in spots if entry <= d <= mark_end and d in ivs)
        if not as_ofs:
            skipped_entries += 1
            continue
        for structure in structures:
            marks = simulate_trajectory(
                structure,
                entry_date=entry,
                as_of_dates=as_ofs,
                spots=spots,
                atm_ivs=ivs,
            )
            # Drop pure insufficient rows (no PnL) so coverage reflects usable marks.
            usable = [m for m in marks if m.data_quality != "insufficient_chain"]
            all_rows.extend(m.to_row(symbol) for m in usable)
    if dry_run:
        return {
            "symbol": symbol,
            "dry_run": True,
            "rows": len(all_rows),
            "skipped_entries": skipped_entries,
            "sample": all_rows[:3],
        }
    n = upsert_marks(conn, all_rows)
    return {
        "symbol": symbol,
        "rows_written": n,
        "rows_computed": len(all_rows),
        "skipped_entries": skipped_entries,
        "iv_observed_days": len(observed_ivs),
        "iv_filled_days": len(ivs),
    }


def run_cohort(
    conn: Any,
    *,
    symbols: Sequence[str],
    lookback_months: int = 6,
    as_of: date | None = None,
    entry_stride_days: int = 5,
    dry_run: bool = False,
    reset: bool = False,
) -> dict[str, Any]:
    end = as_of or date.today()
    start = end - timedelta(days=int(lookback_months * 30.5))
    if reset and not dry_run:
        clear_canonical_pnl_tables(conn)
    results = []
    total = 0
    for sym in symbols:
        spots = fetch_spot_series(conn, sym, start, end)
        days = sorted(spots.keys())
        # Prefer entry dates that already have observed ATM IV (before LOCF).
        observed = fetch_atm_iv_series(conn, sym, start, end)
        iv_days = sorted(d for d in days if d in observed)
        base = iv_days if iv_days else days
        entries = base[:: max(1, entry_stride_days)]
        one = run_symbol_window(
            conn,
            symbol=sym,
            entry_dates=entries,
            as_of_end=end,
            dry_run=dry_run,
        )
        results.append(one)
        total += int(one.get("rows_written") or one.get("rows") or 0)
    cov = None if dry_run else coverage_report(conn)
    return {
        "mode": "cohort",
        "lookback_months": lookback_months,
        "symbols": len(symbols),
        "rows_written": total,
        "per_symbol": results,
        "coverage": cov,
        "dry_run": dry_run,
        "reset": reset,
    }
