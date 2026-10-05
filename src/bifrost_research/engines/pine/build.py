"""Daily Pine signals → ``features.stock_signal_pine_daily``.

For each active script and each universe symbol, send the symbol's adjusted
daily bars to the pine-runner and store the sessions its ``buy`` / ``sell``
plots fired. A script seen for the first time, or whose library version moved,
is rebuilt over the whole history; otherwise only the last ``recent_sessions``
are written, with ``history_days`` of bars sent so the script has its warm-up.

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

from bifrost_research.db.calendar import load_symbols_from_env_or_query
from bifrost_research.db.conn import connect
from bifrost_research.engines.pine import client
from bifrost_research.engines.pine.library import PineScript, ensure_builtins, list_scripts
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_PINE_DAILY

logger = logging.getLogger(__name__)

CHUNK = 100
FULL_HISTORY_DAYS = 365 * 6
HISTORY_DAYS = 600


def load_bars_many(conn: Any, symbols: Sequence[str], start: date, end: date) -> dict[str, list[dict[str, Any]]]:
    """Adjusted daily bars for many symbols, oldest first (live labels; no rename stitching)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT UPPER(symbol), bar_date, open, high, low, close, volume
            FROM raw_market.stock_daily
            WHERE symbol = ANY(%s::text[])
              AND bar_date BETWEEN %s AND %s
              AND close IS NOT NULL
            ORDER BY symbol, bar_date
            """,
            (list(symbols), start, end),
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


def built_versions(conn: Any) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT script_id, MAX(script_version) FROM {TABLE_STOCK_SIGNAL_PINE_DAILY} GROUP BY script_id")
        return {str(a): int(b) for a, b in cur.fetchall() or []}


def signal_rows(
    script: PineScript,
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    fired: Mapping[str, Mapping[str, Any]],
    since: date | None,
) -> tuple[list[tuple[Any, ...]], dict[str, str]]:
    rows: list[tuple[Any, ...]] = []
    errors: dict[str, str] = {}
    for sym, res in fired.items():
        if res.get("error"):
            errors[sym] = str(res["error"])[:300]
            continue
        close_on = {b["date"]: b["close"] for b in bars_by_symbol.get(sym, [])}
        for side in ("buy", "sell"):
            for d in res.get(side) or []:
                if since is not None and d < since:
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
    end = as_of or date.today()
    conn = connect()
    try:
        seeded = 0 if dry_run else ensure_builtins(conn)
        scripts = [s for s in list_scripts(conn, active_only=True) if not script_ids or s.id in script_ids]
        universe = load_symbols_from_env_or_query(conn, symbols=symbols)
        versions = {} if dry_run else built_versions(conn)
        report: dict[str, Any] = {"as_of": end.isoformat(), "seeded": seeded, "symbols": len(universe), "scripts": {}}
        for script in scripts:
            rebuild = full or versions.get(script.id) != script.version
            start = end - timedelta(days=FULL_HISTORY_DAYS if rebuild else HISTORY_DAYS)
            # Calendar days back to cover ``recent_sessions`` sessions with weekends and holidays.
            since = None if rebuild else end - timedelta(days=int(recent_sessions * 1.5) + 4)
            written = 0
            errors: dict[str, str] = {}
            for i in range(0, len(universe), CHUNK):
                chunk = universe[i : i + CHUNK]
                bars = load_bars_many(conn, chunk, start, end)
                if not bars:
                    continue
                try:
                    fired = client.run(script.source, bars)
                except Exception as exc:  # noqa: BLE001 — the runner down is a run failure, said once
                    errors["*"] = f"pine-runner: {type(exc).__name__}: {str(exc)[:200]}"
                    break
                rows, errs = signal_rows(script, bars, fired, since)
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
