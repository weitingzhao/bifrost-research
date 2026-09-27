"""Move a renamed company's derived history onto the symbol it trades under now.

Three US listings in this universe changed ticker without changing company:
EchoStar SATS → ECHO on 2026-06-24, Innovative Solutions & Support ISSC → IA on
2026-08-18, and Equity Residential renaming itself to Vivmark, EQR → VMRK, on
2026-08-18. The Plugin asked the vendor for the old symbol and stored what came
back — the successor's contracts — under the symbol it had asked for, so one
chain arrived split across two labels. Plugin 0.47.0 stops that and moved 23,283
raw rows onto the live symbol; this moves the `features.*` layer that was built
on top of them.

Two situations, and they need opposite treatment. Measured 2026-09-26:

**Before the rename** the old label is not a mistake. `option_daily` holds
32,263 genuinely SATS-rooted rows ending 2026-06-23 and 1,615 ISSC-rooted ones
ending 2026-08-17, and the features built from them are that company's history
under the ticker it had at the time — 1,264 ATM IV rows, 426 IV percentiles and
448 VRP rows for SATS alone. They cannot be recomputed under the new symbol,
because no ECHO-rooted contract exists for those dates and none should. They are
**relabelled**, which is the only way ECHO's IV rank sees more than the 61
percentile rows it has today.

**After the rename** both labels are wrong. The dead-label rows were computed
from the successor's contracts under the wrong name, and the live-label rows
were computed from whatever half of the chain the last job happened to leave
there — for the 2026-10-16 expiry, max pain read 99 over 2,592 open interest
under ECHO against 90 over 59,986 under SATS. So the dead rows are **purged**
and the window is **recomputed** from the repaired raw layer.

Order matters: relabel first so the percentile ranks against the whole history,
then purge, then recompute oldest-first for the same reason.

Every write is scoped to the six symbols. That is not a performance choice —
``iv_history_repair.derive`` deletes a whole session of ``option_metric_atm_iv_daily``
before recomputing the universe it was handed, so calling it with three symbols
would delete 94 sessions of everyone else's ATM IV and rebuild three names' worth.
The per-date compute functions all take an explicit symbol list, so this does its
own deletes and keeps them to the symbols it is moving.

Steps, each counting only unless ``--apply``:

1. relabel   — dead-label rows dated before the handover become live-label rows,
               except on a session the live symbol already has a row for: those
               two cannot both survive the primary key and choosing between them
               would be inventing history. Measured, exactly one table is in that
               state — ``stock_signal_scan_daily`` has twelve rows for both SATS
               and ECHO on the same twelve sessions, 2026-06-08 to 06-23 — so the
               step moves what it can and reports the sessions it left behind
               rather than refusing the table or overwriting one side.
2. purge     — dead-label rows dated on or after the handover. They have no
               correct form: the same contracts are already counted under the
               live label once the recompute runs.
3. recompute — ATM IV → IV percentile → VRP, then max pain, PCR, vol surface,
               momentum and scan, session by session oldest first.

Not covered, and left deliberately rather than half-done: ``option_metric_gex_daily``,
``option_metric_vanna_charm_daily``, ``option_flow_multi_leg_daily``,
``option_flow_sentiment_daily``, ``stock_forecast_terrain_daily``,
``stock_signal_lens_hit_daily``, ``stock_signal_sepa_daily`` and
``option_iv_reconstructed_daily`` have no per-date compute entry point to call with
a symbol list. Their dead-label rows are purged, so nothing is double-counted, but
their live-label rows keep values computed from the torn chain until someone gives
those engines the same per-date door the others have. ``stock_signal_canonical_pnl_daily``
is untouched: it has no ``trade_date`` column and its own repair path.

Usage::

    python -m bifrost_research.engines.rename_history_move
    python -m bifrost_research.engines.rename_history_move --apply
    python -m bifrost_research.engines.rename_history_move --apply --no-recompute
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from typing import Any, Sequence

from bifrost_research.db.conn import connect

logger = logging.getLogger(__name__)

#: The pairs to move. A curated list on purpose: this is a one-off over a set
#: that was measured, and the general rule — root is an active ticker, label is
#: not — lives in the Plugin, which is the side that sees option roots. Deriving
#: it here would need `raw_market.ticker` to hold a row for the dead symbol, and
#: SATS has none: the reference walk only inserts what the vendor lists as
#: active, and SATS was gone before the table was first written.
RENAMES: tuple[tuple[str, str], ...] = (
    ("SATS", "ECHO"),
    ("ISSC", "IA"),
    ("EQR", "VMRK"),
)

#: Every `features.*` table keyed by ``(symbol, trade_date)`` that held a row
#: under one of the dead labels on 2026-09-26.
AFFECTED_TABLES: tuple[str, ...] = (
    "option_metric_atm_iv_daily",
    "option_metric_iv_percentile_daily",
    "option_metric_max_pain_daily",
    "option_metric_pcr_daily",
    "option_metric_vanna_charm_daily",
    "option_metric_gex_daily",
    "option_surface_fit_daily",
    "option_surface_iv_daily",
    "option_surface_residual_daily",
    "option_flow_multi_leg_daily",
    "option_flow_sentiment_daily",
    "option_iv_reconstructed_daily",
    "stock_signal_vrp_daily",
    "stock_signal_momentum_daily",
    "stock_signal_scan_daily",
    "stock_signal_sepa_daily",
    "stock_signal_lens_hit_daily",
    "stock_forecast_terrain_daily",
)

#: The handover: the successor's first close strictly after the old symbol's
#: last one. Not the successor's first close outright — ECHO is a reused ticker
#: (Echo Global Logistics ran 2021-09-09 to 2021-11-22), so an unconditional
#: minimum puts the handover four and a half years early and relabels nothing.
_HANDOVER_SQL = """
SELECT (SELECT max(bar_date) FROM raw_market.stock_daily WHERE symbol = %s) AS dead_last,
       (SELECT min(bar_date) FROM raw_market.stock_daily WHERE symbol = %s
          AND bar_date > (SELECT max(bar_date) FROM raw_market.stock_daily WHERE symbol = %s)) AS alive_first
"""


def _one(cur: Any) -> tuple[Any, ...] | None:
    rows = cur.fetchall() if hasattr(cur, "fetchall") else []
    for row in rows or []:
        return tuple(row.values()) if hasattr(row, "values") else tuple(row)
    return None


def handover(conn: Any, dead: str, alive: str) -> date | None:
    """The first session the live symbol traded after the dead one stopped."""
    with conn.cursor() as cur:
        cur.execute(_HANDOVER_SQL, (dead, alive, dead))
        row = _one(cur)
    if not row or row[1] is None:
        logger.warning("no handover found for %s -> %s; skipping the pair", dead, alive)
        return None
    return row[1]


#: Sessions the live symbol already holds are excluded rather than overwritten.
#: A whole session, not a single row: the primary keys carry a tenor or an expiry
#: beyond ``(symbol, trade_date)``, and refusing the session is the reading that
#: cannot half-move one.
_MOVABLE = """
symbol = %s AND trade_date < %s
  AND NOT EXISTS (
    SELECT 1 FROM features.{table} b
    WHERE b.symbol = %s AND b.trade_date = features.{table}.trade_date
  )
"""


def relabel(conn: Any, *, apply: bool) -> dict[str, Any]:
    """Dead-label rows dated before the handover become the live symbol's."""
    moved: dict[str, int] = {}
    left_behind: list[str] = []
    for dead, alive in RENAMES:
        cut = handover(conn, dead, alive)
        if cut is None:
            continue
        for table in AFFECTED_TABLES:
            movable = _MOVABLE.format(table=table)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT count(*) FROM features.{table} WHERE symbol = %s AND trade_date < %s",
                    (dead, cut),
                )
                n_dead = int((_one(cur) or (0,))[0] or 0)
                if n_dead == 0:
                    continue
                cur.execute(
                    f"SELECT count(*) FROM features.{table} WHERE {movable}",
                    (dead, cut, alive),
                )
                n_move = int((_one(cur) or (0,))[0] or 0)
            if n_move < n_dead:
                left_behind.append(
                    f"{table}:{dead}->{alive} ({n_dead - n_move} row(s) on sessions {alive} already has)"
                )
            if n_move == 0:
                continue
            if apply:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE features.{table} SET symbol = %s WHERE {movable}",
                        (alive, dead, cut, alive),
                    )
                    n_move = int(getattr(cur, "rowcount", 0) or 0)
                conn.commit()
            moved[f"{table}:{dead}->{alive}"] = n_move
    return {
        "step": "relabel",
        "applied": apply,
        "rows": moved,
        "total": sum(moved.values()),
        "left_behind": left_behind,
    }


def purge(conn: Any, *, apply: bool) -> dict[str, Any]:
    """Dead-label rows dated on or after the handover: the torn ones."""
    removed: dict[str, int] = {}
    for dead, alive in RENAMES:
        cut = handover(conn, dead, alive)
        if cut is None:
            continue
        for table in AFFECTED_TABLES:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT count(*) FROM features.{table} WHERE symbol = %s AND trade_date >= %s",
                    (dead, cut),
                )
                n = int((_one(cur) or (0,))[0] or 0)
                if n == 0:
                    continue
                if apply:
                    cur.execute(
                        f"DELETE FROM features.{table} WHERE symbol = %s AND trade_date >= %s",
                        (dead, cut),
                    )
                    n = int(getattr(cur, "rowcount", 0) or 0)
            if apply:
                conn.commit()
            removed[f"{table}:{dead}"] = n
    return {"step": "purge", "applied": apply, "rows": removed, "total": sum(removed.values())}


def sessions_to_recompute(conn: Any, symbols: Sequence[str], start: date) -> list[date]:
    """Trading days from ``start`` that the live symbols have a close for."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT bar_date FROM raw_market.stock_daily "
            "WHERE symbol = ANY(%s) AND bar_date >= %s ORDER BY 1",
            (list(symbols), start),
        )
        rows = cur.fetchall() or []
    return [r[0] if not hasattr(r, "values") else next(iter(r.values())) for r in rows]


def recompute(conn: Any, *, apply: bool) -> dict[str, Any]:
    """Rebuild the live symbols' window from the repaired raw layer, oldest first.

    Oldest first because IV percentile ranks a session against the history
    already rebuilt, which is also why relabel has to have run before this.
    """
    from bifrost_research.engines.momentum.radar import compute_momentum_for_date
    from bifrost_research.engines.scan.entry import compute_scan_for_date
    from bifrost_research.engines.vol_surface.entry import compute_vol_surface_for_date
    from bifrost_research.engines.volatility.atm_iv import compute_atm_iv_for_date
    from bifrost_research.engines.volatility.iv_percentile import compute_iv_percentile_for_date
    from bifrost_research.engines.volatility.max_pain import compute_max_pain_for_date
    from bifrost_research.engines.volatility.pcr import compute_pcr_for_date
    from bifrost_research.engines.vrp.compute import compute_vrp_for_date

    live: list[str] = []
    earliest: date | None = None
    for dead, alive in RENAMES:
        cut = handover(conn, dead, alive)
        if cut is None:
            continue
        live.append(alive)
        earliest = cut if earliest is None else min(earliest, cut)
    if not live or earliest is None:
        return {"step": "recompute", "applied": apply, "sessions": 0}
    days = sessions_to_recompute(conn, live, earliest)
    if not apply:
        return {
            "step": "recompute",
            "applied": False,
            "symbols": sorted(live),
            "sessions": len(days),
            "first": days[0].isoformat() if days else None,
            "last": days[-1].isoformat() if days else None,
        }

    written: dict[str, int] = {}

    def add(key: str, result: Any) -> None:
        written[key] = written.get(key, 0) + int((result or {}).get("rows_written") or 0)

    # The scan engine wants the watchlist it scores against, not a filter.
    for td in days:
        with conn.cursor() as cur:
            for table in ("option_metric_atm_iv_daily", "option_metric_iv_percentile_daily"):
                cur.execute(
                    f"DELETE FROM features.{table} WHERE trade_date = %s AND symbol = ANY(%s)",
                    (td, live),
                )
        conn.commit()
        add("atm_iv", compute_atm_iv_for_date(conn, trade_date=td, underlyings=live))
        add("iv_percentile", compute_iv_percentile_for_date(conn, trade_date=td, underlyings=live))
        add("vrp", compute_vrp_for_date(conn, trade_date=td, underlyings=live))
        add("max_pain", compute_max_pain_for_date(conn, trade_date=td, underlyings=live))
        add("pcr", compute_pcr_for_date(conn, trade_date=td, underlyings=live))
        add("vol_surface", compute_vol_surface_for_date(conn, trade_date=td, underlyings=live))
        add("momentum", compute_momentum_for_date(conn, trade_date=td, symbols=live))
        add("scan", compute_scan_for_date(conn, trade_date=td, watchlist=live, symbols_filter=live))
    return {
        "step": "recompute",
        "applied": True,
        "symbols": sorted(live),
        "sessions": len(days),
        "first": days[0].isoformat(),
        "last": days[-1].isoformat(),
        "rows": written,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; without it every step only counts")
    parser.add_argument("--no-recompute", action="store_true", help="relabel and purge only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = connect()
    try:
        out = {
            "engine": "rename_history_move",
            "renames": [f"{d}->{a}" for d, a in RENAMES],
            "handover": {
                f"{d}->{a}": (h.isoformat() if (h := handover(conn, d, a)) else None) for d, a in RENAMES
            },
            "steps": [relabel(conn, apply=args.apply), purge(conn, apply=args.apply)],
        }
        if not args.no_recompute:
            out["steps"].append(recompute(conn, apply=args.apply))
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))
    # Left-behind sessions are a reported outcome, not a failure: the step did
    # everything that can be done without overwriting a row it did not write.
    return 0


if __name__ == "__main__":
    sys.exit(main())
