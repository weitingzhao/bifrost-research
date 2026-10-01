"""Rebuild what adjusted option contracts reached before 0.154.0 left them out.

0.153.1 kept adjusted contracts (``O:APTV1…``, see ``engines.adjusted_contracts``)
out of the SVI, IV surface and OpEx reads; 0.154.0 does the same for the five
other readers of raw option data. This rebuilds the rows those five wrote while
the contracts were still in, measured read-only on 2026-10-01:

1. reconstructed — ``option_iv_reconstructed_daily`` held 2,385 adjusted-contract
   rows (206 name-sessions, 2026-09-08..30). They are deleted, not recomputed: the
   standard contracts' rows beside them never depended on them.
2. atm_iv        — every stored name-session an adjusted contract could reach:
   the reconstructed rows above, and ``option_daily`` bars from 2024-10-02 on
   (expired adjusted contracts stay there after ``option_contract`` drops them).
   249 of them change. ``compute_atm_iv_for_date`` replaces the session.
3. downstream    — IV percentile and VRP rank today against their own last 252
   stored rows, so a name whose IV30 moved is recomputed, oldest first, on every
   stored session whose window holds a moved IV30.
4. gex / flow / pcr — the stored name-sessions whose open interest (and for PCR,
   whose snapshot volume) held an adjusted contract: 205 / 218 / 233 change.
   Each now replaces its session, so rows built from adjusted contracts alone go.

Only stored name-sessions are touched: a name the engines never wrote (TRP, whose
adjusted bars sit in ``option_daily``) gets no new rows. Without ``--apply`` every
step only counts.

Usage::

    python -m bifrost_research.engines.adjusted_contract_purge
    python -m bifrost_research.engines.adjusted_contract_purge --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date
from typing import Any, Iterable

from bifrost_research.db.conn import connect
from bifrost_research.engines.adjusted_contracts import not_adjusted_contract_sql

logger = logging.getLogger(__name__)

RECON = "features.option_iv_reconstructed_daily"
ATM = "features.option_metric_atm_iv_daily"
PERCENTILE = "features.option_metric_iv_percentile_daily"
VRP = "features.stock_signal_vrp_daily"
WINDOW = 252

Pair = tuple[str, date]


def _adjusted(column: str) -> str:
    return f"NOT ({not_adjusted_contract_sql(column)})"


def _pairs(conn: Any, sql: str) -> set[Pair]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return {(str(r[0]).strip().upper(), r[1]) for r in cur.fetchall() or []}


def _stored(conn: Any, table: str, pairs: Iterable[Pair]) -> set[Pair]:
    pairs = sorted(pairs)
    if not pairs:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT t.symbol, t.trade_date
            FROM {table} t
            JOIN unnest(%s::text[], %s::date[]) AS p(symbol, trade_date)
              ON p.symbol = t.symbol AND p.trade_date = t.trade_date
            """,
            ([s for s, _ in pairs], [d for _, d in pairs]),
        )
        return {(str(r[0]), r[1]) for r in cur.fetchall() or []}


def _by_date(pairs: Iterable[Pair]) -> dict[date, list[str]]:
    out: dict[date, list[str]] = defaultdict(list)
    for sym, td in sorted(pairs):
        out[td].append(sym)
    return dict(sorted(out.items()))


def discover(conn: Any) -> dict[str, set[Pair]]:
    """The name-sessions each reader has stored that an adjusted contract reached."""
    from bifrost_research.engines.volatility.iv_solver import DTE_MAX, DTE_MIN

    recon = _pairs(
        conn,
        f"SELECT DISTINCT symbol, trade_date FROM {RECON} WHERE {_adjusted('option_ticker')}",
    )
    bars = _pairs(
        conn,
        f"""
        SELECT DISTINCT underlying, bar_date FROM raw_market.option_daily
        WHERE {_adjusted('option_ticker')} AND (expiry - bar_date) BETWEEN {DTE_MIN} AND {DTE_MAX}
        """,
    )
    oi = _pairs(
        conn,
        f"""
        SELECT DISTINCT underlying, trade_date FROM raw_market.option_open_interest
        WHERE {_adjusted('option_ticker')}
        """,
    )
    volume = _pairs(
        conn,
        f"""
        SELECT DISTINCT oc.underlying, DATE(timezone('America/New_York', os.snapshot_ts))
        FROM raw_market.option_snapshot os
        JOIN raw_market.option_contract oc ON oc.option_ticker = os.option_ticker
        WHERE {_adjusted('os.option_ticker')}
        """,
    )
    return {
        "recon": recon,
        "atm": _stored(conn, ATM, recon | bars),
        "gex": _stored(conn, "features.option_metric_gex_levels_daily", oi),
        "flow": _stored(conn, "features.option_flow_sentiment_daily", oi),
        "pcr": _stored(conn, "features.option_metric_pcr_daily", oi | volume),
    }


def iv30_by_pair(conn: Any, pairs: Iterable[Pair]) -> dict[Pair, float | None]:
    """IV30 as ``iv30_from_expiries`` reads it, per stored name-session."""
    from bifrost_research.engines.volatility.atm_iv import iv30_from_expiries

    pairs = sorted(pairs)
    if not pairs:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.symbol, t.trade_date, t.expiry, t.atm_iv
            FROM {ATM} t
            JOIN unnest(%s::text[], %s::date[]) AS p(symbol, trade_date)
              ON p.symbol = t.symbol AND p.trade_date = t.trade_date
            WHERE t.atm_iv IS NOT NULL
            """,
            ([s for s, _ in pairs], [d for _, d in pairs]),
        )
        rows = cur.fetchall() or []
    expiries: dict[Pair, list[tuple[Any, Any]]] = defaultdict(list)
    for sym, td, expiry, iv in rows:
        expiries[(str(sym), td)].append((expiry, iv))
    return {p: iv30_from_expiries(p[1], expiries.get(p, [])) for p in pairs}


def downstream_sessions(conn: Any, moved: Iterable[Pair]) -> set[Pair]:
    """Stored IV percentile / VRP sessions whose 252-row window holds a moved IV30."""
    by_sym: dict[str, list[date]] = defaultdict(list)
    for sym, td in moved:
        by_sym[sym].append(td)
    out: set[Pair] = set()
    for sym, days in sorted(by_sym.items()):
        first = min(days)
        for table in (PERCENTILE, VRP):
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT trade_date FROM {table} WHERE symbol = %s AND trade_date >= %s ORDER BY trade_date",
                    (sym, first),
                )
                stored = [r[0] for r in cur.fetchall() or []]
            for i, td in enumerate(stored):
                lo = stored[max(0, i - (WINDOW - 1))]
                if any(lo <= d <= td for d in days):
                    out.add((sym, td))
    return out


def _moved(before: dict[Pair, float | None], after: dict[Pair, float | None]) -> set[Pair]:
    out = set()
    for p, a in before.items():
        b = after.get(p)
        if (a is None) != (b is None) or (a is not None and b is not None and abs(a - b) > 1e-9):
            out.add(p)
    return out


def run(conn: Any, *, apply: bool) -> dict[str, Any]:
    from bifrost_research.engines.flow import compute_order_flow_for_symbol
    from bifrost_research.engines.gex.exposure import compute_gex_for_symbol
    from bifrost_research.engines.volatility.atm_iv import compute_atm_iv_for_date
    from bifrost_research.engines.volatility.iv_percentile import compute_iv_percentile_for_date
    from bifrost_research.engines.volatility.pcr import compute_pcr_for_date
    from bifrost_research.engines.vrp.compute import compute_vrp_for_date

    found = discover(conn)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {RECON} WHERE {_adjusted('option_ticker')}")
        recon_rows = int(cur.fetchone()[0])
    summary: dict[str, Any] = {
        "applied": apply,
        "reconstructed": {"rows": recon_rows, "sessions": len(found["recon"])},
        **{
            k: {
                "sessions": len(found[k]),
                "symbols": len({s for s, _ in found[k]}),
                "first": min((d for _, d in found[k]), default=None),
                "last": max((d for _, d in found[k]), default=None),
            }
            for k in ("atm", "gex", "flow", "pcr")
        },
    }
    if not apply:
        return summary

    # 1. reconstructed: the adjusted rows go; nothing beside them depended on them.
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {RECON} WHERE {_adjusted('option_ticker')}")
        summary["reconstructed"]["deleted"] = int(cur.rowcount or 0)
    conn.commit()

    # 2. ATM IV, session by session over the names each session stored.
    before = iv30_by_pair(conn, found["atm"])
    for td, syms in _by_date(found["atm"]).items():
        compute_atm_iv_for_date(conn, trade_date=td, underlyings=syms)
    after = iv30_by_pair(conn, found["atm"])
    moved = _moved(before, after)
    summary["atm"]["iv30_moved"] = len(moved)

    # 3. IV percentile and VRP, oldest first, wherever a moved IV30 is in the window.
    down = downstream_sessions(conn, moved)
    written = {"iv_percentile": 0, "vrp": 0}
    for td, syms in _by_date(down).items():
        with conn.cursor() as cur:
            for table in (PERCENTILE, VRP):
                cur.execute(f"DELETE FROM {table} WHERE trade_date = %s AND symbol = ANY(%s)", (td, syms))
        conn.commit()
        written["iv_percentile"] += int(
            compute_iv_percentile_for_date(conn, trade_date=td, underlyings=syms).get("rows_written") or 0
        )
        written["vrp"] += int(compute_vrp_for_date(conn, trade_date=td, underlyings=syms).get("rows_written") or 0)
    summary["downstream"] = {
        "sessions": len(down),
        "symbols": len({s for s, _ in down}),
        "rows_written": written,
    }

    # 4. GEX, flow and PCR replace their sessions themselves now.
    summary["gex"]["ok"] = sum(
        bool(compute_gex_for_symbol(conn, symbol=s, trade_date=td).get("ok")) for s, td in sorted(found["gex"])
    )
    summary["flow"]["ok"] = sum(
        bool(compute_order_flow_for_symbol(conn, symbol=s, trade_date=td).get("ok")) for s, td in sorted(found["flow"])
    )
    summary["pcr"]["rows_written"] = sum(
        int(compute_pcr_for_date(conn, trade_date=td, underlyings=syms).get("rows_written") or 0)
        for td, syms in _by_date(found["pcr"]).items()
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; without it every step only counts")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = connect()
    try:
        with conn.cursor() as cur:
            # The discovery scans walk option_daily (42M rows) and every snapshot partition.
            cur.execute("SET statement_timeout = '300s'")
            if not args.apply:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        conn.commit()
        summary = run(conn, apply=args.apply)
    finally:
        conn.close()
    print(json.dumps(summary, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
