"""Best-effort context gatherers for Morning / EOD agents (graceful if empty)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def _safe(fn, *args, default: Any = None, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent context gather failed: %s", exc)
        return default


def gather_symbol_context(conn: _Connection, symbols: list[str]) -> dict[str, Any]:
    """Pull latest VRP / terrain regime / event radar hits per symbol (best-effort)."""
    out: dict[str, Any] = {"symbols": {}, "as_of": date.today().isoformat()}
    if not symbols:
        return out

    from bifrost_research.repositories import vrp as vrp_repo

    for sym in symbols[:8]:
        bucket: dict[str, Any] = {"symbol": sym}
        row = _safe(vrp_repo.get_latest, conn, sym, default=None)
        if row:
            bucket["vrp"] = {
                "trade_date": row.get("trade_date"),
                "vrp_20d": row.get("vrp_20d"),
                "vrp_pct_252d": row.get("vrp_pct_252d"),
                "atm_iv_30d": row.get("atm_iv_30d"),
                "rv_20d": row.get("rv_20d"),
            }
        bucket["regime"] = _safe(_latest_regime, conn, sym, default=None)
        bucket["events"] = _safe(_recent_events, conn, sym, default=[])
        out["symbols"][sym] = bucket
    return out


def _latest_regime(conn: _Connection, symbol: str) -> dict[str, Any] | None:
    sql = """
        SELECT symbol, trade_date, regime, spot, pin_score, vol_squeeze
        FROM features.stock_forecast_terrain_daily
        WHERE UPPER(TRIM(symbol)) = %s
        ORDER BY trade_date DESC
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (symbol.strip().upper(),))
        row = cur.fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        d = dict(row)
    else:
        cols = ("symbol", "trade_date", "regime", "spot", "pin_score", "vol_squeeze")
        d = {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
    td = d.get("trade_date")
    if isinstance(td, (date, datetime)):
        d["trade_date"] = td.date().isoformat() if isinstance(td, datetime) else td.isoformat()
    return d


def _recent_events(conn: _Connection, symbol: str, *, limit: int = 3) -> list[dict[str, Any]]:
    sql = """
        SELECT event_id, title, event_type, importance, collected_at, direction
        FROM features.event_signal_radar_daily
        WHERE UPPER(affected_symbols) LIKE %s
          AND COALESCE(dropped, false) = false
        ORDER BY collected_at DESC NULLS LAST, importance DESC NULLS LAST
        LIMIT %s
    """
    like = f"%{symbol.strip().upper()}%"
    with conn.cursor() as cur:
        cur.execute(sql, (like, int(limit)))
        rows = cur.fetchall() or []
    result: list[dict[str, Any]] = []
    cols = ("event_id", "title", "event_type", "importance", "collected_at", "direction")
    for row in rows:
        if hasattr(row, "keys"):
            d = {c: row[c] for c in cols if c in row.keys()}
        else:
            d = {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
        ca = d.get("collected_at")
        if isinstance(ca, (date, datetime)):
            d["collected_at"] = ca.isoformat() if hasattr(ca, "isoformat") else str(ca)
        result.append(d)
    return result


def gather_discoveries(conn: _Connection, *, limit: int = 5) -> list[dict[str, Any]]:
    """Top SEPA / VRP extremes / event radar not yet claimed — best-effort."""
    discoveries: list[dict[str, Any]] = []

    sepa = _safe(_top_sepa, conn, limit=limit, default=[])
    for row in sepa:
        discoveries.append({"source": "sepa", **row})

    if len(discoveries) < limit:
        events = _safe(_top_events, conn, limit=limit - len(discoveries), default=[])
        for row in events:
            discoveries.append({"source": "event_radar", **row})

    return discoveries[:limit]


def _top_sepa(conn: _Connection, *, limit: int = 5) -> list[dict[str, Any]]:
    sql = """
        SELECT symbol, trade_date, sepa_score, grade, stage
        FROM features.stock_signal_sepa_daily
        WHERE sepa_score IS NOT NULL
        ORDER BY trade_date DESC, sepa_score DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (int(limit),))
        rows = cur.fetchall() or []
    cols = ("symbol", "trade_date", "sepa_score", "grade", "stage")
    out: list[dict[str, Any]] = []
    for row in rows:
        if hasattr(row, "keys"):
            d = {c: row[c] for c in cols if c in row.keys()}
        else:
            d = {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
        td = d.get("trade_date")
        if isinstance(td, (date, datetime)):
            d["trade_date"] = td.isoformat() if hasattr(td, "isoformat") else str(td)
        out.append(d)
    return out


def _top_events(conn: _Connection, *, limit: int = 5) -> list[dict[str, Any]]:
    sql = """
        SELECT event_id, title, event_type, importance, affected_symbols, collected_at
        FROM features.event_signal_radar_daily
        WHERE COALESCE(dropped, false) = false
        ORDER BY collected_at DESC NULLS LAST, importance DESC NULLS LAST
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (int(limit),))
        rows = cur.fetchall() or []
    cols = (
        "event_id",
        "title",
        "event_type",
        "importance",
        "affected_symbols",
        "collected_at",
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        if hasattr(row, "keys"):
            d = {c: row[c] for c in cols if c in row.keys()}
        else:
            d = {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
        ca = d.get("collected_at")
        if isinstance(ca, (date, datetime)):
            d["collected_at"] = ca.isoformat() if hasattr(ca, "isoformat") else str(ca)
        out.append(d)
    return out


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
