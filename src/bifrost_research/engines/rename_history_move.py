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
3. recompute — reconstructed IV first, then session by session oldest first:
               ATM IV → IV percentile → VRP, max pain, PCR, vol surface,
               vanna/charm, momentum, scan; then the gex, flow and terrain slots;
               then lens hits.
4. drop-colliding (``--drop-colliding``) — the dead-label rows on sessions the live
               symbol already holds. Only ``stock_signal_scan_daily`` is in that
               state, and it is lossless there: measured 2026-09-26, both symbols'
               twelve rows for 2026-06-08..06-23 carry a composite score and
               nothing else — no close, no IV rank, no terrain, no gex on either
               side. **Measure before using this again**: another collision may
               have the information on the dead side.

Every affected table is recomputed. The first version of this said eight of them
had "no per-date compute entry point", which was looking in the wrong layer: the
engines expose no ``compute_*_for_date``, but ``scheduler.engines.run_slot`` takes
an explicit ``symbols`` list and its per-slot runners take ``trading_days``, so gex,
flow and terrain were scoped all along. Vanna/charm comes from
``compute_opex_for_date``, reconstructed IV from ``iv_solver.run_cohort``, and lens
hits from ``signal_hit.run``, which scopes itself from ``RESEARCH_WATCHLIST``.

Order follows the dependencies: reconstructed IV feeds ATM IV, so it goes first;
gex feeds terrain; lens hits read the tables the rest of the pass writes, so they
go last.

``stock_signal_sepa_daily`` needs nothing beyond the purge — SEPA is projected from
the dbt stock marts, not from an option chain, so a torn chain never reached it.
``stock_signal_canonical_pnl_daily`` is untouched: no ``trade_date`` column and its
own repair path.

Usage::

    python -m bifrost_research.engines.rename_history_move
    python -m bifrost_research.engines.rename_history_move --apply
    python -m bifrost_research.engines.rename_history_move --apply --no-recompute
    python -m bifrost_research.engines.rename_history_move --apply --drop-colliding
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


def drop_colliding(conn: Any, *, apply: bool) -> dict[str, Any]:
    """Dead-label rows on sessions the live symbol already holds.

    ``relabel`` leaves these rather than choosing between two rows one primary key
    cannot hold. Measured 2026-09-26, the only table in that state is
    ``stock_signal_scan_daily``, and neither side carries more than a composite
    score for those twelve sessions — no close, no IV rank, no terrain, no gex —
    so dropping the dead one loses nothing. That measurement is the reason this
    is a separate flag and not part of relabel: a later collision may hold the
    information on the dead side, and then this would destroy it.
    """
    removed: dict[str, int] = {}
    for dead, alive in RENAMES:
        cut = handover(conn, dead, alive)
        if cut is None:
            continue
        for table in AFFECTED_TABLES:
            sql_where = (
                f"symbol = %s AND trade_date < %s AND EXISTS ("
                f"SELECT 1 FROM features.{table} b WHERE b.symbol = %s "
                f"AND b.trade_date = features.{table}.trade_date)"
            )
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM features.{table} WHERE {sql_where}", (dead, cut, alive))
                n = int((_one(cur) or (0,))[0] or 0)
                if n == 0:
                    continue
                if apply:
                    cur.execute(f"DELETE FROM features.{table} WHERE {sql_where}", (dead, cut, alive))
                    n = int(getattr(cur, "rowcount", 0) or 0)
            if apply:
                conn.commit()
            removed[f"{table}:{dead}"] = n
    return {"step": "drop_colliding", "applied": apply, "rows": removed, "total": sum(removed.values())}


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


def earliest_option_bar(conn: Any, symbol: str) -> date | None:
    """The first session the live symbol has an option bar for.

    Plugin 0.51.0 moved the pre-rename option history onto the successor, so the
    live symbol now owns the whole series and a recompute can reach all of it —
    which is better than the relabelled rows it replaces, because the same
    contracts produce it and a later run reproduces the same answer.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(bar_date) FROM raw_market.option_daily WHERE underlying = %s",
            (symbol,),
        )
        row = _one(cur)
    return row[0] if row and row[0] else None


def recompute(conn: Any, *, apply: bool, from_earliest: bool = False) -> dict[str, Any]:
    """Rebuild the live symbols' window from the repaired raw layer, oldest first.

    Oldest first because IV percentile ranks a session against the history
    already rebuilt, which is also why relabel has to have run before this.

    **Each symbol gets its own start.** Taking the earliest handover across every
    pair and applying it to all of them deleted rows one symbol could not
    reproduce: the first run recomputed IA from 2026-06-24, which is SATS's
    handover and seven weeks before IA's, so fifteen ATM IV rows that had just
    been relabelled from ISSC were deleted with nothing to write back.

    ``from_earliest`` starts each symbol at its first option bar instead of at
    its handover, which is reachable now that the raw history sits under the live
    symbol. It replaces relabelled rows with recomputed ones — the same
    contracts, but an answer a later run can reproduce.
    """
    import os

    from bifrost_research.engines.momentum.radar import compute_momentum_for_date
    from bifrost_research.engines.opex_cycle.entry import compute_opex_for_date
    from bifrost_research.engines.scan.entry import compute_scan_for_date
    from bifrost_research.engines.signal_hit import entry as signal_hit_entry
    from bifrost_research.engines.vol_surface.entry import compute_vol_surface_for_date
    from bifrost_research.engines.volatility.atm_iv import compute_atm_iv_for_date
    from bifrost_research.engines.volatility.iv_percentile import compute_iv_percentile_for_date
    from bifrost_research.engines.volatility.iv_solver import run_cohort as iv_solver_cohort
    from bifrost_research.engines.volatility.max_pain import compute_max_pain_for_date
    from bifrost_research.engines.volatility.pcr import compute_pcr_for_date
    from bifrost_research.engines.vrp.compute import compute_vrp_for_date
    from bifrost_research.scheduler.engines import run_slot

    starts: dict[str, date] = {}
    for dead, alive in RENAMES:
        cut = handover(conn, dead, alive)
        if cut is None:
            continue
        start = cut
        if from_earliest:
            first_bar = earliest_option_bar(conn, alive)
            if first_bar is not None:
                start = min(start, first_bar)
        starts[alive] = min(starts[alive], start) if alive in starts else start
    if not starts:
        return {"step": "recompute", "applied": apply, "sessions": 0}

    # One pass per session over the symbols that session belongs to, so a symbol
    # is never asked about a date before it existed.
    per_day: dict[date, list[str]] = {}
    for alive, start in starts.items():
        for td in sessions_to_recompute(conn, [alive], start):
            per_day.setdefault(td, []).append(alive)
    days = sorted(per_day)
    if not apply:
        return {
            "step": "recompute",
            "applied": False,
            "from_earliest": from_earliest,
            "starts": {k: v.isoformat() for k, v in sorted(starts.items())},
            "sessions": len(days),
            "first": days[0].isoformat() if days else None,
            "last": days[-1].isoformat() if days else None,
        }

    written: dict[str, int] = {}
    live_all = sorted(starts)

    def add(key: str, result: Any) -> None:
        written[key] = written.get(key, 0) + int((result or {}).get("rows_written") or 0)

    # Reconstructed IV feeds ATM IV, so it is rebuilt before the session loop
    # rather than after it.
    add("iv_reconstructed", iv_solver_cohort(conn, symbols=live_all, lookback_days=len(days), as_of=days[-1]))

    for td in days:
        syms = sorted(per_day[td])
        with conn.cursor() as cur:
            for table in ("option_metric_atm_iv_daily", "option_metric_iv_percentile_daily"):
                cur.execute(
                    f"DELETE FROM features.{table} WHERE trade_date = %s AND symbol = ANY(%s)",
                    (td, syms),
                )
        conn.commit()
        add("atm_iv", compute_atm_iv_for_date(conn, trade_date=td, underlyings=syms))
        add("iv_percentile", compute_iv_percentile_for_date(conn, trade_date=td, underlyings=syms))
        add("vrp", compute_vrp_for_date(conn, trade_date=td, underlyings=syms))
        add("max_pain", compute_max_pain_for_date(conn, trade_date=td, underlyings=syms))
        add("pcr", compute_pcr_for_date(conn, trade_date=td, underlyings=syms))
        # vol_surface reports its rows under its own keys, not rows_written, so
        # this counter reads 0 for it; the table is the thing to check.
        add("vol_surface", compute_vol_surface_for_date(conn, trade_date=td, underlyings=syms))
        add("vanna_charm", compute_opex_for_date(conn, trade_date=td, underlyings=syms))
        add("momentum", compute_momentum_for_date(conn, trade_date=td, symbols=syms))
        add("scan", compute_scan_for_date(conn, trade_date=td, watchlist=syms, symbols_filter=syms))

    # These three take an explicit symbol list through run_slot rather than a
    # per-date function. gex feeds terrain, so the order is not alphabetical.
    # None of them deletes, so a session a symbol has no data for is counted as
    # skipped rather than writing over anything.
    slots: dict[str, Any] = {}
    for slot in ("gex", "flow", "terrain"):
        result = run_slot(slot, symbols=live_all, lookback_days=len(days), as_of=days[-1])
        slots[slot] = {k: result.get(k) for k in ("rows_written", "symbols_ok", "symbols_failed", "skipped")}

    # Lens hits read what the rest of the pass wrote, so they go last. signal_hit
    # scopes itself from RESEARCH_WATCHLIST — it takes no symbol argument — and
    # the explicit lists above are unaffected by the variable being set.
    prev_watch = os.environ.get("RESEARCH_WATCHLIST")
    os.environ["RESEARCH_WATCHLIST"] = ",".join(live_all)
    try:
        slots["signal_hit"] = {
            k: signal_hit_entry.run(lookback_days=len(days), as_of=days[-1]).get(k)
            for k in ("rows_written",)
        }
    finally:
        if prev_watch is None:
            os.environ.pop("RESEARCH_WATCHLIST", None)
        else:
            os.environ["RESEARCH_WATCHLIST"] = prev_watch
    return {
        "step": "recompute",
        "applied": True,
        "from_earliest": from_earliest,
        "starts": {k: v.isoformat() for k, v in sorted(starts.items())},
        "sessions": len(days),
        "first": days[0].isoformat(),
        "last": days[-1].isoformat(),
        "rows": written,
        "slots": slots,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; without it every step only counts")
    parser.add_argument("--no-recompute", action="store_true", help="relabel and purge only")
    parser.add_argument(
        "--drop-colliding",
        action="store_true",
        help="also delete dead-label rows on sessions the live symbol already has",
    )
    parser.add_argument(
        "--from-earliest",
        action="store_true",
        help="recompute each symbol from its first option bar, not from its handover",
    )
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
        if args.drop_colliding:
            out["steps"].append(drop_colliding(conn, apply=args.apply))
        if not args.no_recompute:
            out["steps"].append(
                recompute(conn, apply=args.apply, from_earliest=args.from_earliest)
            )
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))
    # Left-behind sessions are a reported outcome, not a failure: the step did
    # everything that can be done without overwriting a row it did not write.
    return 0


if __name__ == "__main__":
    sys.exit(main())
