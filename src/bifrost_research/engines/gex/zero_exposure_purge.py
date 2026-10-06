"""Remove the GEX levels written for expiries without gamma exposure (TD-136).

Until 0.185.0 ``compute_gex_for_symbol`` wrote a levels row for every expiry it
had contracts for. Where the strike distribution carries no gamma exposure at all
(open interest all zero, or a few contracts so far from spot that their gamma
rounds to nothing), both walls fall on the first strike listed and zero gamma on
the strike nearest spot. Measured read-only on 2026-10-06: 1,534 such rows
(``call_wall_gex`` and ``put_wall_gex`` both zero) on 1,186 name-sessions and 496
names, 2026-07-06..10-05, 24,413 distribution rows beside them.

1. gex       — the levels rows and their distribution rows are deleted, in one
               transaction. Nothing else is recomputed in GEX: the other expiries
               of those sessions never depended on them.
2. terrain   — daily rows whose GEX input was one of them (the newest levels row
               at or before the session, nearest expiry): 104 measured. Recomputed
               the way the daily slot writes them, after the delete.
3. scan      — rows whose GEX column read one of them (the expiry nearest 30 days
               on the session: 117 measured) or whose terrain was recomputed.

The intraday terrain is a snapshot by ``asof_ts`` and is left alone; signal_hit's
GEX lens is rebuilt by its Sunday full re-walk. Without ``--apply`` every step only
counts.

``--one-sided`` runs the 0.191.0 pass instead (TD-157): a levels row whose one side
has no gamma exposure still named a wall for that side, the first strike listed
with wall gex 0. Measured 2026-10-06 after the pass above: 1,762 rows (820 call
side, 942 put side) on 754 name-sessions and 244 names; terrain read 237 of them.
That wall and its gex become NULL; terrain and scan are recomputed on the
name-sessions whose terrain read one.

``--zero-gamma`` runs the 0.192.0 pass (TD-166): zero gamma is recomputed for every
daily levels row from its stored distribution with ``zero_gamma_crossing``, which
counts only a change of sign as a crossing; without one the row stores NULL (the
fallback was the strike nearest spot). The old rule reproduces every stored value
from the distribution (10,718 of 10,718 on three sample days), so the pass is
exact. Measured 2026-10-06: 26,518 of 69,440 rows had no crossing, and a few a
real crossing beside a "crossing" out of zero that sat nearer spot. Terrain is
recomputed on the name-sessions whose GEX input changed, scan on those and on the
sessions whose 30-day expiry changed.

Usage::

    python -m bifrost_research.engines.gex.zero_exposure_purge
    python -m bifrost_research.engines.gex.zero_exposure_purge --apply
    python -m bifrost_research.engines.gex.zero_exposure_purge --one-sided [--apply]
    python -m bifrost_research.engines.gex.zero_exposure_purge --zero-gamma [--apply]
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import sys
from collections import defaultdict
from datetime import date
from typing import Any

from bifrost_research.db.conn import connect
from bifrost_research.engines.adjusted_contract_purge import Pair, _by_date, _pairs, _stored

logger = logging.getLogger(__name__)

LEVELS = "features.option_metric_gex_levels_daily"
DIST = "features.option_metric_gex_daily"
TERRAIN = "features.stock_forecast_terrain_daily"
SCAN = "features.stock_signal_scan_daily"

#: The row ``has_gamma_exposure`` would no longer write.
NO_EXPOSURE = "COALESCE({t}.call_wall_gex, 0) = 0 AND COALESCE({t}.put_wall_gex, 0) = 0"
#: A wall named on a side without exposure (``drop_empty_side_walls`` writes NULL).
EMPTY_CALL_WALL = "{t}.major_call_wall IS NOT NULL AND COALESCE({t}.call_wall_gex, 0) = 0"
EMPTY_PUT_WALL = "{t}.major_put_wall IS NOT NULL AND COALESCE({t}.put_wall_gex, 0) = 0"
ONE_SIDED = f"(({EMPTY_CALL_WALL}) OR ({EMPTY_PUT_WALL}))"

_TERRAIN_READS = f"""
    SELECT t.symbol, t.trade_date
    FROM {TERRAIN} t
    CROSS JOIN LATERAL (
        SELECT g.major_call_wall, g.major_put_wall, g.call_wall_gex, g.put_wall_gex
        FROM {LEVELS} g
        WHERE g.symbol = t.symbol AND g.trade_date <= t.trade_date
        ORDER BY g.trade_date DESC, g.expiry ASC
        LIMIT 1
    ) g
    WHERE {{where}}
"""
_TERRAIN_SQL = _TERRAIN_READS.format(where=NO_EXPOSURE.format(t="g"))

# The scan's gex_30d: the expiry nearest 30 days on the session itself.
_SCAN_SQL = f"""
    WITH src AS (
        SELECT DISTINCT ON (symbol, trade_date) symbol, trade_date, call_wall_gex, put_wall_gex
        FROM {LEVELS}
        WHERE spot IS NOT NULL AND spot > 0
        ORDER BY symbol, trade_date, ABS((expiry - trade_date) - 30) ASC, expiry ASC
    )
    SELECT s.symbol, s.trade_date
    FROM {SCAN} s
    JOIN src ON src.symbol = s.symbol AND src.trade_date = s.trade_date
    WHERE {NO_EXPOSURE.format(t="src")}
"""


def _scalars(conn: Any, sql: str) -> tuple[Any, ...]:
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return tuple(row or ())


def run(conn: Any, *, apply: bool) -> dict[str, Any]:
    rows, sessions, names = _scalars(
        conn,
        f"""
        SELECT COUNT(*), COUNT(DISTINCT (symbol, trade_date)), COUNT(DISTINCT symbol)
        FROM {LEVELS} l WHERE {NO_EXPOSURE.format(t="l")}
        """,
    )
    (dist_rows,) = _scalars(
        conn,
        f"""
        SELECT COUNT(*) FROM {DIST} d
        JOIN {LEVELS} l ON l.symbol = d.symbol AND l.trade_date = d.trade_date AND l.expiry = d.expiry
        WHERE {NO_EXPOSURE.format(t="l")}
        """,
    )
    terrain: set[Pair] = _pairs(conn, _TERRAIN_SQL)
    scan: set[Pair] = _pairs(conn, _SCAN_SQL)
    summary: dict[str, Any] = {
        "applied": apply,
        "gex": {"levels_rows": int(rows), "sessions": int(sessions), "symbols": int(names), "dist_rows": int(dist_rows)},
        "terrain": {"sessions": len(terrain)},
        "scan": {"sessions": len(scan | terrain), "gex_read": len(scan)},
    }
    if not apply:
        return summary

    # 1. GEX: distribution rows first (they are found through the levels rows).
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {DIST} d USING {LEVELS} l
            WHERE l.symbol = d.symbol AND l.trade_date = d.trade_date AND l.expiry = d.expiry
              AND {NO_EXPOSURE.format(t="l")}
            """
        )
        summary["gex"]["dist_deleted"] = int(cur.rowcount or 0)
        cur.execute(f"DELETE FROM {LEVELS} l WHERE {NO_EXPOSURE.format(t='l')}")
        summary["gex"]["levels_deleted"] = int(cur.rowcount or 0)
    if summary["gex"]["levels_deleted"] != int(rows):
        conn.rollback()
        raise RuntimeError(f"counted {rows} levels rows, the delete touched {summary['gex']['levels_deleted']}")
    conn.commit()

    # 2–3. Terrain on today's inputs, then the scan rows that read either.
    summary["terrain"]["rows_written"], summary["scan"]["rows_written"] = _recompute(conn, terrain, scan | terrain)
    return summary


def _recompute(conn: Any, terrain: set[Pair], scan: set[Pair]) -> tuple[int, int]:
    """Terrain the way the daily slot writes it, then scan; returns rows written."""
    from bifrost_research.engines.forecast.terrain import (
        compute_market_terrain,
        load_upstream_signals,
        upsert_market_terrain,
    )
    from bifrost_research.engines.scan.entry import compute_scan_for_date

    written = 0
    for sym, td in sorted(terrain):
        spot, gex, momentum, iv = load_upstream_signals(conn, sym, td)
        if spot <= 0:
            continue
        row = compute_market_terrain(sym, td, spot=spot, gex=gex or None, momentum=momentum or None, iv=iv or None)
        written += upsert_market_terrain(conn, [row])
    scan_rows = 0
    for td, syms in _by_date(scan).items():
        scan_rows += int(
            compute_scan_for_date(conn, trade_date=td, watchlist=syms, symbols_filter=syms).get("rows_written") or 0
        )
    return written, scan_rows


def run_one_sided(conn: Any, *, apply: bool) -> dict[str, Any]:
    """TD-157: NULL the wall named on a side without exposure; terrain and scan after."""
    call_rows, put_rows, sessions, names = _scalars(
        conn,
        f"""
        SELECT COUNT(*) FILTER (WHERE {EMPTY_CALL_WALL.format(t="l")}),
               COUNT(*) FILTER (WHERE {EMPTY_PUT_WALL.format(t="l")}),
               COUNT(DISTINCT (symbol, trade_date)), COUNT(DISTINCT symbol)
        FROM {LEVELS} l WHERE {ONE_SIDED.format(t="l")}
        """,
    )
    terrain: set[Pair] = _pairs(conn, _TERRAIN_READS.format(where=ONE_SIDED.format(t="g")))
    scan = _stored(conn, SCAN, terrain)
    summary: dict[str, Any] = {
        "applied": apply,
        "gex": {"call_walls": int(call_rows), "put_walls": int(put_rows), "sessions": int(sessions), "symbols": int(names)},
        "terrain": {"sessions": len(terrain)},
        "scan": {"sessions": len(scan)},
    }
    if not apply:
        return summary

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {LEVELS} l SET major_call_wall = NULL, call_wall_gex = NULL WHERE {EMPTY_CALL_WALL.format(t='l')}"
        )
        summary["gex"]["call_walls_cleared"] = int(cur.rowcount or 0)
        cur.execute(
            f"UPDATE {LEVELS} l SET major_put_wall = NULL, put_wall_gex = NULL WHERE {EMPTY_PUT_WALL.format(t='l')}"
        )
        summary["gex"]["put_walls_cleared"] = int(cur.rowcount or 0)
    if (summary["gex"]["call_walls_cleared"], summary["gex"]["put_walls_cleared"]) != (int(call_rows), int(put_rows)):
        conn.rollback()
        raise RuntimeError(
            f"counted {call_rows}/{put_rows} walls, the update touched "
            f"{summary['gex']['call_walls_cleared']}/{summary['gex']['put_walls_cleared']}"
        )
    conn.commit()
    summary["terrain"]["rows_written"], summary["scan"]["rows_written"] = _recompute(conn, terrain, scan)
    return summary


Key = tuple[str, date, date]  # (symbol, session, expiry)


def _zero_gamma_changes(conn: Any) -> tuple[dict[Key, float | None], dict[Key, float], int]:
    """New zero gamma for every levels row whose value changes; also each row's spot."""
    from bifrost_research.engines.gex.exposure_guards import zero_gamma_crossing

    with conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT trade_date FROM {LEVELS} ORDER BY 1")
        days = [r[0] for r in cur.fetchall() or []]
    changes: dict[Key, float | None] = {}
    spots: dict[Key, float] = {}
    rows = 0
    for td in days:
        with conn.cursor() as cur:
            cur.execute(f"SELECT symbol, expiry, spot, zero_gamma FROM {LEVELS} WHERE trade_date = %s", (td,))
            levels = cur.fetchall() or []
            cur.execute(
                f"SELECT symbol, expiry, strike, net_gex FROM {DIST} WHERE trade_date = %s ORDER BY symbol, expiry, strike",
                (td,),
            )
            dist_rows = cur.fetchall() or []
        dist: dict[tuple[str, date], list[dict[str, float]]] = defaultdict(list)
        for sym, exp, strike, net in dist_rows:
            dist[(str(sym), exp)].append({"strike": float(strike), "net_gex": float(net or 0)})
        for sym, exp, spot, zg in levels:
            rows += 1
            key = (str(sym), td, exp)
            spots[key] = float(spot or 0)
            new = zero_gamma_crossing(dist.get((str(sym), exp), []), float(spot or 0))
            old = None if zg is None else float(zg)
            if (new is None) != (old is None) or (new is not None and old is not None and abs(new - old) > 1e-6):
                changes[key] = new
    return changes, spots, rows


def _readers(conn: Any, changed: set[Key], spots: dict[Key, float]) -> tuple[set[Pair], set[Pair]]:
    """Terrain rows whose GEX input (newest session at or before, nearest expiry) and
    scan rows whose 30-day expiry is a changed row; scan also takes terrain's."""
    by_sym: dict[str, dict[date, list[date]]] = defaultdict(lambda: defaultdict(list))
    for sym, td, exp in spots:
        by_sym[sym][td].append(exp)
    dates = {sym: sorted(v) for sym, v in by_sym.items()}
    with conn.cursor() as cur:
        cur.execute(f"SELECT symbol, trade_date FROM {TERRAIN}")
        terrain_rows = cur.fetchall() or []
        cur.execute(f"SELECT symbol, trade_date FROM {SCAN}")
        scan_rows = cur.fetchall() or []
    terrain: set[Pair] = set()
    for sym, td in terrain_rows:
        ds = dates.get(str(sym))
        i = bisect.bisect_right(ds, td) - 1 if ds else -1
        if i >= 0:
            src = ds[i]
            if (str(sym), src, min(by_sym[str(sym)][src])) in changed:
                terrain.add((str(sym), td))
    scan: set[Pair] = set()
    for sym, td in scan_rows:
        exps = [e for e in by_sym.get(str(sym), {}).get(td, []) if spots.get((str(sym), td, e), 0) > 0]
        if exps:
            near = min(exps, key=lambda e: (abs((e - td).days - 30), e))
            if (str(sym), td, near) in changed:
                scan.add((str(sym), td))
    scan_keys = {(str(s), d) for s, d in scan_rows}
    return terrain, scan | (terrain & scan_keys)


def run_zero_gamma(conn: Any, *, apply: bool) -> dict[str, Any]:
    """TD-166: zero gamma only at a change of sign; NULL without one."""
    changes, spots, rows = _zero_gamma_changes(conn)
    terrain, scan = _readers(conn, set(changes), spots)
    to_null = sum(1 for v in changes.values() if v is None)
    summary: dict[str, Any] = {
        "applied": apply,
        "gex": {
            "levels_rows": rows,
            "changed": len(changes),
            "to_null": to_null,
            "moved_crossing": len(changes) - to_null,
            "symbols": len({k[0] for k in changes}),
        },
        "terrain": {"sessions": len(terrain)},
        "scan": {"sessions": len(scan)},
    }
    if not apply:
        return summary

    keys = sorted(changes)
    touched = 0
    with conn.cursor() as cur:
        for lo in range(0, len(keys), 5000):
            chunk = keys[lo : lo + 5000]
            cur.execute(
                f"""
                UPDATE {LEVELS} l SET zero_gamma = v.zg
                FROM unnest(%s::text[], %s::date[], %s::date[], %s::float8[]) AS v(symbol, trade_date, expiry, zg)
                WHERE l.symbol = v.symbol AND l.trade_date = v.trade_date AND l.expiry = v.expiry
                """,
                ([k[0] for k in chunk], [k[1] for k in chunk], [k[2] for k in chunk], [changes[k] for k in chunk]),
            )
            touched += int(cur.rowcount or 0)
    if touched != len(keys):
        conn.rollback()
        raise RuntimeError(f"counted {len(keys)} zero-gamma changes, the update touched {touched}")
    conn.commit()
    summary["gex"]["updated"] = touched
    summary["terrain"]["rows_written"], summary["scan"]["rows_written"] = _recompute(conn, terrain, scan)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; without it every step only counts")
    parser.add_argument("--one-sided", action="store_true", help="the 0.191.0 pass: walls on a side without exposure")
    parser.add_argument("--zero-gamma", action="store_true", help="the 0.192.0 pass: zero gamma only at a change of sign")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '300s'")
            if not args.apply:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        conn.commit()
        if args.zero_gamma:
            summary = run_zero_gamma(conn, apply=args.apply)
        elif args.one_sided:
            summary = run_one_sided(conn, apply=args.apply)
        else:
            summary = run(conn, apply=args.apply)
    finally:
        conn.close()
    print(json.dumps(summary, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
