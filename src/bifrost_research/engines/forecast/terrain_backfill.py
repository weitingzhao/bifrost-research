"""Terrain for the days the Owner actually opened positions — measure first, then fill.

``features.stock_forecast_terrain_daily`` only ever held what the nightly slot
wrote, so a closed instance opened in March has no regime next to it and every
post-mortem reads "regime: —". This module fills those specific sessions.

Measure first, because the nightly slot cannot: ``load_upstream_signals`` takes
the newest input row *at or before* the date it is asked for, and skips a
(symbol, date) only when spot is missing. Asked for a date before GEX / momentum
/ IV coverage begins it therefore returns empty dicts, and the terrain engine
happily scores a regime out of defaults. Backfilling that way would fill the
column with numbers that came from nothing.

So a target is only computed when all three signal inputs exist for that symbol
within ``MAX_INPUT_STALENESS_DAYS`` of the session, and spot is real. Everything
else is reported as a skip with its reason — a gap that is visible is worth more
than a regime that is invented.

Targets come from the Trade API over HTTP (executions plus the current position
attribution): the opening date of every strategy instance that no longer holds a
position, and the underlyings that instance traded. D10 BLOCKED — advisory only.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from bifrost_research.db.conn import connect
from bifrost_research.engines.forecast.terrain import (
    compute_market_terrain,
    load_upstream_signals,
    upsert_market_terrain,
)
from bifrost_research.engines.option_pinned.entry import underlying_of

logger = logging.getLogger(__name__)

#: The three signal tables terrain scores a regime from. Spot comes from
#: ``raw_market.stock_daily`` (or the index fallback) and is checked separately.
SIGNAL_INPUTS: tuple[tuple[str, str], ...] = (
    ("gex", "features.option_metric_gex_levels_daily"),
    ("momentum", "features.stock_signal_momentum_daily"),
    ("iv", "features.option_metric_iv_percentile_daily"),
)
STOCK_DAILY = "raw_market.stock_daily"
#: An input row more than a week older than the session is not that session's reading.
MAX_INPUT_STALENESS_DAYS = 7
EXECUTIONS_LIMIT = 10000


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ── what to fill (Trade API, read-only HTTP) ──────────────────────────────


def _open_date(rows: Sequence[Mapping[str, Any]]) -> date | None:
    for row in rows:
        epoch = row.get("strategy_instance_opened_at_epoch")
        if epoch:
            try:
                return datetime.fromtimestamp(float(epoch), tz=timezone.utc).date()
            except (TypeError, ValueError, OSError):
                pass
    dates = [str(r.get("trade_date"))[:10] for r in rows if r.get("trade_date")]
    parsed = []
    for raw in dates:
        try:
            parsed.append(date.fromisoformat(raw))
        except ValueError:
            continue
    return min(parsed) if parsed else None


def instance_targets(get: Any, base: str) -> tuple[list[tuple[str, date]], dict[str, int]]:
    """``[(underlying, opened_on)]`` for every instance that no longer holds a position."""
    attribution = get(base, "/executions/position-attribution") or {}
    still_open = {
        row.get("strategy_instance_id")
        for row in attribution.get("attributions") or []
        if row.get("strategy_instance_id") is not None
    }
    payload = get(base, "/executions", {"since_ts": 0, "limit": EXECUTIONS_LIMIT}) or {}
    by_instance: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for row in payload.get("executions") or []:
        sid = row.get("strategy_instance_id")
        if sid is not None:
            by_instance[sid].append(row)
    targets: set[tuple[str, date]] = set()
    closed = 0
    for sid, rows in by_instance.items():
        if sid in still_open:
            continue
        closed += 1
        opened = _open_date(rows)
        if opened is None:
            continue
        for row in rows:
            underlying = underlying_of(row.get("symbol"))
            if underlying:
                targets.add((underlying, opened))
    stats = {
        "instances_with_executions": len(by_instance),
        "instances_holding": len(still_open),
        "closed_instances": closed,
        "targets": len(targets),
    }
    return sorted(targets), stats


# ── how deep the inputs go ────────────────────────────────────────────────


def input_floors(conn: Any) -> dict[str, str | None]:
    """The earliest ``trade_date`` in each terrain input — the wall the backfill stops at."""
    out: dict[str, str | None] = {}
    with conn.cursor() as cur:
        for name, table in SIGNAL_INPUTS:
            cur.execute(f"SELECT MIN(trade_date) FROM {table}")
            row = cur.fetchone()
            out[name] = row[0].isoformat() if row and row[0] else None
        cur.execute(f"SELECT MIN(bar_date) FROM {STOCK_DAILY}")
        row = cur.fetchone()
        out["stock_daily"] = row[0].isoformat() if row and row[0] else None
    return out


def latest_input_dates(conn: Any, symbol: str, on: date) -> dict[str, date | None]:
    """Per input, the newest ``trade_date`` at or before ``on`` for this symbol."""
    out: dict[str, date | None] = {}
    with conn.cursor() as cur:
        for name, table in SIGNAL_INPUTS:
            cur.execute(
                f"SELECT MAX(trade_date) FROM {table} WHERE symbol = %s AND trade_date <= %s",
                (symbol, on),
            )
            row = cur.fetchone()
            out[name] = row[0] if row and row[0] else None
    return out


def usable(dates: Mapping[str, date | None], on: date) -> list[str]:
    """Names of the inputs that cannot support a regime for ``on``."""
    cutoff = on - timedelta(days=MAX_INPUT_STALENESS_DAYS)
    missing: list[str] = []
    for name, _table in SIGNAL_INPUTS:
        seen = dates.get(name)
        if seen is None or seen < cutoff:
            missing.append(name)
    return missing


# ── the fill ──────────────────────────────────────────────────────────────


def backfill(conn: Any, targets: Iterable[tuple[str, date]]) -> dict[str, Any]:
    """Write terrain for the targets whose inputs are really there; report the rest."""
    written = 0
    covered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for symbol, on in targets:
        dates = latest_input_dates(conn, symbol, on)
        missing = usable(dates, on)
        if missing:
            reasons[f"missing:{'+'.join(missing)}"] += 1
            skipped.append({"symbol": symbol, "trade_date": on.isoformat(), "missing": missing})
            continue
        spot, gex, momentum, iv = load_upstream_signals(conn, symbol, on)
        if spot <= 0:
            reasons["no_spot"] += 1
            skipped.append({"symbol": symbol, "trade_date": on.isoformat(), "missing": ["spot"]})
            continue
        terrain = compute_market_terrain(
            symbol, on, spot=spot, gex=gex or None, momentum=momentum or None, iv=iv or None
        )
        written += upsert_market_terrain(conn, [terrain])
        covered.append({"symbol": symbol, "trade_date": on.isoformat(), "regime": terrain.regime})
    conn.commit()
    total = len(covered) + len(skipped)
    return {
        "rows_written": written,
        "covered": len(covered),
        "skipped": len(skipped),
        "coverage": round(len(covered) / total, 4) if total else None,
        "skip_reasons": dict(reasons),
        "covered_sample": covered[:50],
        "skipped_sample": skipped[:50],
    }


def run(*, as_of: date | None = None) -> dict[str, Any]:
    from bifrost_research.mcp.tools._trade_api_client import base_trading, get

    day = as_of or _today()
    try:
        targets, target_stats = instance_targets(get, base_trading())
    except Exception as exc:  # noqa: BLE001 — no target list, no backfill
        logger.warning("terrain backfill skipped: trade api unavailable: %s", exc)
        return {
            "engine": "terrain_backfill",
            "as_of": day.isoformat(),
            "mode": "skipped",
            "reason": f"trade api unavailable: {exc}",
            "rows_written": 0,
        }
    conn = connect()
    try:
        floors = input_floors(conn)
        result = backfill(conn, targets)
    finally:
        conn.close()
    return {
        "engine": "terrain_backfill",
        "as_of": day.isoformat(),
        "mode": "written",
        "input_floors": floors,
        "max_input_staleness_days": MAX_INPUT_STALENESS_DAYS,
        **target_stats,
        **result,
    }
