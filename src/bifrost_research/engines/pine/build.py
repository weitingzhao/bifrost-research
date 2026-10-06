"""Daily Pine signals → ``features.stock_signal_pine_daily``.

For each active script and each universe symbol, send the symbol's adjusted
daily bars to the pine-runner and store the sessions its ``buy`` / ``sell``
plots fired. A script seen for the first time, or whose library version moved,
is rebuilt over the whole history; otherwise only the last ``recent_sessions``
are written, with ``history_days`` of bars sent so the script has its warm-up.

What a signal row means (0.175.0):

- **Warm-up.** No signal is stored from a symbol's first ``WARMUP_BARS`` (100)
  bars in the series sent: indicators there run on too little history (an
  Ichimoku cloud needs 78 bars; an RMA-based ATR is still ~1% off its settled
  value after 100 bars of a 22-bar length). A full rebuild starts at the first
  bar the feed has, so the earliest ~100 sessions of every name are dropped; an
  incremental run sends ``HISTORY_DAYS`` (~410 bars) and only a name listed for
  fewer than 100 sessions loses anything. One fixed N for every script, not a
  per-script declaration: the eight built-ins all settle well inside it.
- **Renames.** A renamed company's bars are spliced across the handover
  (``listing_lineage.spliced_bars_sql``) and stored under its live symbol, so
  its signals run straight through the rename instead of restarting cold.
- **Delisted names.** A full rebuild also runs the names that have left the
  universe because they stopped trading (``retired_names``: inactive in
  ``raw_market.ticker``, with option history in the window), so a signal
  study is not limited to the survivors (B7). Incremental runs skip them:
  they print no new bars.
- **Adjustment basis.** Bars are the feed's adjusted closes as of the day the
  row was written. An incremental run rewrites only the last few sessions, so
  after a split or a large dividend the older rows of that name were computed
  on a different basis than the new ones until the next full rebuild.

Usage::

    python -m bifrost_research.engines.pine.build
    python -m bifrost_research.engines.pine.build --symbol NVDA --script supertrend --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from bifrost_research.db.calendar import load_symbols_from_env_or_query, ny_today
from bifrost_research.db.conn import connect
from bifrost_research.engines.pine import client
from bifrost_research.engines.pine.library import PineScript, ensure_builtins, list_scripts
from bifrost_research.repositories.listing_lineage import spliced_bars_sql
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_PINE_DAILY

logger = logging.getLogger(__name__)

CHUNK = 100
FULL_HISTORY_DAYS = 365 * 6
HISTORY_DAYS = 600
WARMUP_BARS = 100


def load_bars_many(conn: Any, symbols: Sequence[str], start: date, end: date) -> dict[str, list[dict[str, Any]]]:
    """Adjusted daily bars for many symbols, oldest first, keyed by live label (renames spliced)."""
    label_expr, keep_clause, params, tickers = spliced_bars_sql(conn, list(symbols))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT UPPER({label_expr}) AS sym, bar_date, open, high, low, close, volume
            FROM raw_market.stock_daily
            WHERE symbol = ANY(%(tickers)s::text[]) AND {keep_clause}
              AND bar_date BETWEEN %(start)s AND %(end)s
              AND close IS NOT NULL
            ORDER BY sym, bar_date
            """,
            {**params, "tickers": tickers, "start": start, "end": end},
        )
        rows = cur.fetchall() or []
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if isinstance(r, Mapping):
            r = tuple(r.values())
        sym, d, o, h, low, c, v = r
        if isinstance(d, datetime):
            d = d.date()
        out.setdefault(sym, []).append(
            {
                "date": d,
                "open": float(o) if o is not None else None,
                "high": float(h) if h is not None else None,
                "low": float(low) if low is not None else None,
                "close": float(c),
                "volume": float(v) if v is not None else None,
            }
        )
    return out


def retired_names(conn: Any, universe: Sequence[str], start: date) -> list[str]:
    """Names outside ``universe`` that stopped trading and had options in the window.

    ``research.option_universe`` keeps only today's members, so a name that
    left by delisting has no row there. Having ``option_daily`` bars since
    ``start`` is the closest record of having been in the option universe
    (on 2026-10-06: 692 underlyings since 2024-10, 686 of them current members,
    4 of the rest inactive — CRNX, WBS, AVB, EA).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.symbol
            FROM (SELECT DISTINCT ON (symbol) symbol, active FROM raw_market.ticker ORDER BY symbol) AS t
            WHERE t.active IS FALSE
              AND NOT (t.symbol = ANY(%(universe)s::text[]))
              AND EXISTS (SELECT 1 FROM raw_market.option_daily o
                          WHERE o.underlying = t.symbol AND o.bar_date >= %(start)s)
              AND EXISTS (SELECT 1 FROM raw_market.stock_daily d
                          WHERE d.symbol = t.symbol AND d.bar_date >= %(start)s AND d.close > 0)
            ORDER BY 1
            """,
            {"universe": list(universe), "start": start},
        )
        return [str(r[0]).upper() for r in cur.fetchall() or []]


def built_versions(conn: Any) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT script_id, MAX(script_version) FROM {TABLE_STOCK_SIGNAL_PINE_DAILY} GROUP BY script_id")
        return {str(a): int(b) for a, b in cur.fetchall() or []}


def signal_rows(
    script: PineScript,
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    fired: Mapping[str, Mapping[str, Any]],
    since: date | None,
    *,
    warmup_bars: int = 0,
) -> tuple[list[tuple[Any, ...]], dict[str, str]]:
    """Rows to write: signals on or after ``since`` and past the first ``warmup_bars`` bars sent."""
    rows: list[tuple[Any, ...]] = []
    errors: dict[str, str] = {}
    for sym, res in fired.items():
        if res.get("error"):
            errors[sym] = str(res["error"])[:300]
            continue
        sent = bars_by_symbol.get(sym, [])
        close_on = {b["date"]: b["close"] for b in sent}
        warm_from = sent[warmup_bars]["date"] if warmup_bars and len(sent) > warmup_bars else None
        for side in ("buy", "sell"):
            for d in res.get(side) or []:
                if since is not None and d < since:
                    continue
                if warmup_bars and (warm_from is None or d < warm_from):
                    continue
                rows.append((script.id, sym, d, side, script.version, close_on.get(d)))
    return rows, errors


def _write(conn: Any, script_id: str, rows: Sequence[tuple[Any, ...]], *, replace_from: date | None, symbols: Sequence[str]) -> None:
    with conn.cursor() as cur:
        if replace_from is None:
            cur.execute(
                f"DELETE FROM {TABLE_STOCK_SIGNAL_PINE_DAILY} WHERE script_id = %s AND symbol = ANY(%s::text[])",
                (script_id, list(symbols)),
            )
        else:
            cur.execute(
                f"""DELETE FROM {TABLE_STOCK_SIGNAL_PINE_DAILY}
                    WHERE script_id = %s AND symbol = ANY(%s::text[]) AND trade_date >= %s""",
                (script_id, list(symbols), replace_from),
            )
        if rows:
            cur.executemany(
                f"""INSERT INTO {TABLE_STOCK_SIGNAL_PINE_DAILY}
                    (script_id, symbol, trade_date, side, script_version, close)
                    VALUES (%s, %s, %s, %s, %s, %s)""",
                list(rows),
            )
    conn.commit()


def run(
    *,
    as_of: date | None = None,
    symbols: Sequence[str] | None = None,
    script_ids: Sequence[str] | None = None,
    recent_sessions: int = 10,
    full: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    end = as_of or ny_today()
    conn = connect()
    try:
        seeded = 0 if dry_run else ensure_builtins(conn)
        scripts = [s for s in list_scripts(conn, active_only=True) if not script_ids or s.id in script_ids]
        universe = load_symbols_from_env_or_query(conn, symbols=symbols)
        versions = {} if dry_run else built_versions(conn)
        report: dict[str, Any] = {"as_of": end.isoformat(), "seeded": seeded, "symbols": len(universe), "scripts": {}}
        retired: list[str] | None = None
        for script in scripts:
            rebuild = full or versions.get(script.id) != script.version
            start = end - timedelta(days=FULL_HISTORY_DAYS if rebuild else HISTORY_DAYS)
            names = list(universe)
            if rebuild and not symbols:
                if retired is None:
                    retired = retired_names(conn, universe, start)
                    report["retired_names"] = retired
                names += retired
            # Calendar days back to cover ``recent_sessions`` sessions with weekends and holidays.
            since = None if rebuild else end - timedelta(days=int(recent_sessions * 1.5) + 4)
            written = 0
            errors: dict[str, str] = {}
            for i in range(0, len(names), CHUNK):
                chunk = names[i : i + CHUNK]
                bars = load_bars_many(conn, chunk, start, end)
                if not bars:
                    continue
                try:
                    fired = client.run(script.source, bars)
                except Exception as exc:  # noqa: BLE001 — the runner down is a run failure, said once
                    errors["*"] = f"pine-runner: {type(exc).__name__}: {str(exc)[:200]}"
                    break
                rows, errs = signal_rows(script, bars, fired, since, warmup_bars=WARMUP_BARS)
                errors.update(errs)
                if not dry_run:
                    _write(conn, script.id, rows, replace_from=since, symbols=list(bars))
                written += len(rows)
            report["scripts"][script.id] = {
                "version": script.version,
                "mode": "rebuild" if rebuild else "recent",
                "rows": written,
                "errors": len(errors),
                "error_sample": dict(list(errors.items())[:3]),
            }
        report["advisory"] = "D10 BLOCKED — signals only, no order path"
        return report
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--as-of", type=date.fromisoformat)
    ap.add_argument("--symbol", action="append")
    ap.add_argument("--script", action="append")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    out = run(as_of=a.as_of, symbols=a.symbol, script_ids=a.script, full=a.full, dry_run=a.dry_run)
    print(json.dumps(out, default=str, indent=2))
    return 1 if any(v["errors"] for v in out["scripts"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
