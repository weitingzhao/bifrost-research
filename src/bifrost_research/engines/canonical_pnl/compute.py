"""Canonical PnL engine: load spots/IV, simulate, dual-write features + dw_stock mart."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from bifrost_research.db.fastcount import breakdown_with_dominant, distinct_count, estimate_rows
from bifrost_research.engines.volatility.atm_iv import IV30_MAX_DTE, IV30_MIN_DTE, iv30_from_expiries
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

# Entry dates are the first session with IV in each 7-day block counted from this
# Monday (a 5-session stride = one entry per ISO week). Until 0.117.0 they were every
# 5th IV day from the window's first day, which slides a day each night: 09-24 and
# 09-25 shared none of PLTR's 26 entries, and with rows never pruned every night wrote
# a fresh 2.3M-row phase once the IV repair gave the universe six months of history.
ENTRY_ANCHOR = date(2024, 1, 1)
# run_symbol_window marks an entry for this many calendar days; after that its rows
# are final and a nightly run need not recompute them.
MARK_HORIZON_DAYS = 60

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
    """IV30 per session (``atm_iv.iv30_from_expiries``, the reading VRP and the IV
    percentile use); VRP's stored atm_iv_30d when the ATM table has nothing.

    Until 0.111.0 this took the single expiry nearest 30 days — a third "current IV"
    beside the other two, and on fossil days a deep ITM/OTM contract's.
    """
    out: dict[date, float] = {}
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""
                SELECT trade_date, expiry, atm_iv::float
                FROM features.option_metric_atm_iv_daily
                WHERE symbol = %s AND trade_date BETWEEN %s AND %s
                  AND atm_iv IS NOT NULL AND atm_iv > 0
                  AND (expiry - trade_date) BETWEEN {IV30_MIN_DTE} AND {IV30_MAX_DTE}
                ORDER BY trade_date
                """,
                (symbol.upper(), start, end),
            )
            by_day: dict[date, list[tuple[Any, Any]]] = {}
            for d, expiry, iv in cur.fetchall():
                by_day.setdefault(d, []).append((expiry, iv))
            for d, pairs in by_day.items():
                iv30 = iv30_from_expiries(d, pairs)
                if iv30 is not None:
                    out[d] = _normalize_iv(iv30)
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


#: The data_quality nearly every row carries; the rest are counted off a
#: partial index (0.116.0).
DOMINANT_QUALITY = "iv_interpolated"


def _coverage_fast(conn: Any) -> dict[str, Any]:
    """The same report without a full scan (0.116.0) — rows are the planner's estimate."""
    table = TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY
    with conn.cursor() as cur:
        total, estimated = estimate_rows(cur, table)
        by_q = breakdown_with_dominant(cur, table, "data_quality", DOMINANT_QUALITY, total)
        symbols = distinct_count(cur, table, "symbol")
        entry_dates = distinct_count(cur, table, "entry_date")
    insuff = int(by_q.get("insufficient_chain") or 0)
    return {
        "symbols": symbols,
        "entry_dates": entry_dates,
        "rows": total,
        "rows_estimated": estimated,
        "by_quality": by_q,
        "insufficient_pct": (insuff / total) if total else None,
        "mart_table": TABLE_MART_CANONICAL_PNL_DAILY,
        "features_table": TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY,
        "dw_schema": SCHEMA_DW_STOCK,
    }


def coverage_report(conn: Any, *, fast: bool = False) -> dict[str, Any]:
    """Coverage of the canonical P&L features table.

    ``fast`` reads it off indexes and the planner's row estimate — what a
    reader-facing endpoint under the 2s statement timeout needs. Without it
    the counts are exact full scans, which a batch run's own summary wants
    right after it writes (before autoanalyze has refreshed the estimate).
    """
    if fast:
        return _coverage_fast(conn)
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


def phase_entries(iv_days: Sequence[date], *, start: date, stride: int = 5) -> list[date]:
    """First IV day in each block of ``stride`` sessions (7 calendar days per 5) counted
    from ``ENTRY_ANCHOR``. A block that begins before ``start`` is left out: its first
    day may sit outside the window, and taking the first day inside would move with it."""
    block = max(1, round(max(1, int(stride)) * 7 / 5))
    out: list[date] = []
    seen: set[int] = set()
    for d in sorted(iv_days):
        b = (d - ENTRY_ANCHOR).days // block
        if b in seen:
            continue
        seen.add(b)
        if ENTRY_ANCHOR + timedelta(days=b * block) >= start:
            out.append(d)
    return out


def compute_marks(
    conn: Any,
    *,
    symbol: str,
    entry_dates: Sequence[date],
    as_of_end: date,
    structures: Sequence[StructureName] = STRUCTURES,
    iv_max_gap_days: int = 14,
) -> dict[str, Any]:
    """Marks for ``entry_dates`` through ``as_of_end`` (each entry for at most
    ``MARK_HORIZON_DAYS``); nothing is written."""
    start = min(entry_dates)
    spots = fetch_spot_series(conn, symbol, start, as_of_end)
    observed_ivs = fetch_atm_iv_series(conn, symbol, start, as_of_end)
    # Carry sparse reconstructed / vendor IV onto the spot calendar (IDS-4).
    ivs = locf_fill_iv(observed_ivs, sorted(spots.keys()), max_gap_days=iv_max_gap_days)
    rows: list[dict[str, Any]] = []
    skipped = 0
    for entry in entry_dates:
        if entry not in ivs or entry not in spots:
            skipped += 1
            continue
        mark_end = min(as_of_end, entry + timedelta(days=MARK_HORIZON_DAYS))
        as_ofs = sorted(d for d in spots if entry <= d <= mark_end and d in ivs)
        if not as_ofs:
            skipped += 1
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
            rows.extend(m.to_row(symbol) for m in marks if m.data_quality != "insufficient_chain")
    return {
        "rows": rows,
        "skipped": skipped,
        "iv_observed_days": len(observed_ivs),
        "iv_filled_days": len(ivs),
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
    out = compute_marks(
        conn,
        symbol=symbol,
        entry_dates=entry_dates,
        as_of_end=as_of_end,
        structures=structures,
        iv_max_gap_days=iv_max_gap_days,
    )
    all_rows = out["rows"]
    if dry_run:
        return {
            "symbol": symbol,
            "dry_run": True,
            "rows": len(all_rows),
            "skipped_entries": out["skipped"],
            "sample": all_rows[:3],
        }
    n = upsert_marks(conn, all_rows)
    return {
        "symbol": symbol,
        "rows_written": n,
        "rows_computed": len(all_rows),
        "skipped_entries": out["skipped"],
        "iv_observed_days": out["iv_observed_days"],
        "iv_filled_days": out["iv_filled_days"],
    }


def simulate_entry(
    conn: Any,
    *,
    symbol: str,
    entry_date: date,
    structure: StructureName,
    as_of_end: date,
) -> dict[str, Any]:
    """One entry's trajectory computed on request, from the series and simulator the
    nightly run uses. ``entry_date`` moves to the first session on or after it, so a
    hypothesis opened on a weekend still has an entry. Stored rows exist only on the
    weekly phase: 2 of 72 hypotheses found theirs on 2026-09-25."""
    sym = symbol.strip().upper()
    sessions = sorted(fetch_spot_series(conn, sym, entry_date, entry_date + timedelta(days=10)))
    entry = next((d for d in sessions if d >= entry_date), None)
    if entry is None or entry > as_of_end:
        return {"entry_date": None, "rows": []}
    out = compute_marks(conn, symbol=sym, entry_dates=[entry], as_of_end=as_of_end, structures=[structure])
    return {"entry_date": entry, "rows": out["rows"]}


def mark_row_json(row: Mapping[str, Any]) -> dict[str, Any]:
    """A stored or simulated mark row as the trajectory routes return it."""
    item = dict(row)
    for k in ("as_of_date", "entry_date"):
        if isinstance(item.get(k), date):
            item[k] = item[k].isoformat()
    return item


def prune_symbol(conn: Any, symbol: str, *, start: date, entries: Sequence[date]) -> int:
    """Drop a symbol's rows outside the window or off the entry phase."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY}
            WHERE symbol = %s AND (entry_date < %s OR NOT (entry_date = ANY(%s)))
            """,
            (symbol.upper(), start, list(entries)),
        )
        return int(cur.rowcount or 0)


def run_cohort(
    conn: Any,
    *,
    symbols: Sequence[str],
    lookback_months: int = 6,
    as_of: date | None = None,
    entry_stride_days: int = 5,
    dry_run: bool = False,
    reset: bool = False,
    coverage: bool = True,
    refresh_days: int | None = None,
) -> dict[str, Any]:
    """Canonical marks per symbol over the window, on the fixed entry phase.

    ``refresh_days`` recomputes only entries that young — the nightly run passes
    ``MARK_HORIZON_DAYS``, past which an entry's marks are final; None recomputes every
    entry (a rebuild). Unless dry, each symbol's rows outside the window or off the
    phase go first, so the table holds exactly the window.
    """
    end = as_of or date.today()
    start = end - timedelta(days=int(lookback_months * 30.5))
    if reset and not dry_run:
        clear_canonical_pnl_tables(conn)
    results = []
    total = 0
    pruned = 0
    for sym in symbols:
        spots = fetch_spot_series(conn, sym, start, end)
        observed = fetch_atm_iv_series(conn, sym, start, end)
        iv_days = sorted(d for d in spots if d in observed)
        entries = phase_entries(iv_days, start=start, stride=entry_stride_days)
        if refresh_days is None:
            todo = entries
        else:
            todo = [e for e in entries if e >= end - timedelta(days=refresh_days)]
        if not dry_run:
            pruned += prune_symbol(conn, sym, start=start, entries=entries)
        one = run_symbol_window(
            conn,
            symbol=sym,
            entry_dates=todo,
            as_of_end=end,
            dry_run=dry_run,
        )
        if not dry_run:
            conn.commit()  # the prune stands even when no entry needed marks
        results.append(one)
        total += int(one.get("rows_written") or one.get("rows") or 0)
    cov = coverage_report(conn) if coverage and not dry_run else None
    return {
        "mode": "cohort",
        "lookback_months": lookback_months,
        "refresh_days": refresh_days,
        "symbols": len(symbols),
        "rows_written": total,
        "rows_pruned": pruned,
        "per_symbol": results,
        "coverage": cov,
        "dry_run": dry_run,
        "reset": reset,
    }
