"""Materialized scanner HTTP routes — Analyze Wave D/H.

GET /research/scan — query ``features.stock_signal_scan_daily``.
Wave H adds composite presets + per-lens flag query params.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.engines.scan.build import (
    normalize_atm_slope_score,
    normalize_pin_score,
)
from bifrost_research.schema.schemas import (
    TABLE_STOCK_SIGNAL_LENS_HIT_DAILY,
    TABLE_STOCK_SIGNAL_SCAN_DAILY,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/scan", tags=["research-scan"])

SORT_COLUMNS = frozenset(
    {
        "composite_score",
        "iv_rank_1y",
        "vrp_pct_252d",
        "atm_slope_30d",
        "pin_pct_distance",
        "pin_score",
        "tail_risk",
        "trend_release",
        "gex_notional",
        "zero_gamma_offset",
        "symbol",
        "close",
    }
)

FLAG_KEYS = frozenset({"iv_rank", "vrp", "atm_slope", "pin", "terrain"})
FLAG_VALUES = frozenset({"hot", "cold", "neutral"})

PresetName = Literal["neutral", "momentum", "mean_revert", "adaptive_30d"]

# Percent weights (sum 100). Stored composite_score uses neutral.
COMPOSITE_PRESETS: dict[str, dict[str, float]] = {
    "neutral": {"iv_rank": 25, "vrp": 25, "atm_slope": 15, "pin": 15, "terrain": 20},
    "momentum": {"iv_rank": 15, "vrp": 15, "atm_slope": 30, "pin": 10, "terrain": 30},
    "mean_revert": {"iv_rank": 35, "vrp": 30, "atm_slope": 10, "pin": 15, "terrain": 10},
}


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def parse_flag_filter(raw: str | None) -> list[tuple[str, str]]:
    """Parse ``iv_rank:hot,vrp:hot`` into ANDed (key, value) pairs."""
    if not raw or not raw.strip():
        return []
    pairs: list[tuple[str, str]] = []
    for segment in raw.split(","):
        part = segment.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"invalid flag filter segment: {part!r}")
        key, value = part.split(":", 1)
        key = key.strip()
        value = value.strip().lower()
        if key not in FLAG_KEYS:
            raise ValueError(f"unknown flag key: {key!r}")
        if value not in FLAG_VALUES:
            raise ValueError(f"unknown flag value: {value!r}")
        pairs.append((key, value))
    return pairs


def merge_flag_filters(
    flag_filter: str | None,
    *,
    iv_rank: str | None = None,
    vrp: str | None = None,
    atm_slope: str | None = None,
    pin: str | None = None,
    terrain: str | None = None,
) -> list[tuple[str, str]]:
    """Combine legacy ``flag_filter`` with per-lens query params (AND)."""
    pairs = parse_flag_filter(flag_filter)
    for key, raw in (
        ("iv_rank", iv_rank),
        ("vrp", vrp),
        ("atm_slope", atm_slope),
        ("pin", pin),
        ("terrain", terrain),
    ):
        if raw is None or not str(raw).strip():
            continue
        value = str(raw).strip().lower()
        if value == "all":
            continue
        if value not in FLAG_VALUES:
            raise ValueError(f"unknown flag value for {key}: {value!r}")
        pairs.append((key, value))
    # Deduplicate keeping last value per key (query params win over flag_filter)
    by_key: dict[str, str] = {}
    for key, value in pairs:
        by_key[key] = value
    return list(by_key.items())


def resolve_sort_column(sort_by: str) -> str:
    column = sort_by.strip()
    if column not in SORT_COLUMNS:
        raise ValueError(f"unsupported sort_by: {column!r}")
    return column



def load_adaptive_weights(conn: Any, *, window_days: int = 30) -> dict[str, float]:
    """Normalize 30d hit_rate_5d across iv_rank/vrp/opex_pin into percent weights.

    Maps opex_pin → pin weight; terrain keeps a small floor so composite stays defined.
    """
    from datetime import timedelta

    sql = f"""
        SELECT lens, trigger_side, hit_5d
        FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY}
        WHERE trade_date >= CURRENT_DATE - %s
          AND hit_5d IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql, (window_days + 5,))
        rows = cur.fetchall() or []

    # Aggregate per lens
    buckets: dict[str, list[bool]] = {"iv_rank": [], "vrp": [], "opex_pin": []}
    for row in rows:
        lens = row[0]
        hit = row[2]
        if lens in buckets and hit is not None:
            buckets[lens].append(bool(hit))

    rates: dict[str, float] = {}
    for lens, hits in buckets.items():
        if hits:
            rates[lens] = sum(1 for h in hits if h) / len(hits)
        else:
            rates[lens] = 0.5  # neutral prior

    # Map to composite keys; keep atm_slope + terrain floors
    raw = {
        "iv_rank": rates["iv_rank"],
        "vrp": rates["vrp"],
        "atm_slope": 0.5,
        "pin": rates["opex_pin"],
        "terrain": 0.5,
    }
    total = sum(raw.values()) or 1.0
    return {k: round(v / total * 100.0, 2) for k, v in raw.items()}


def resolve_preset(preset: str) -> tuple[str, dict[str, float] | None]:
    name = (preset or "neutral").strip().lower()
    if name not in COMPOSITE_PRESETS and name != "adaptive_30d":
        raise ValueError(f"unsupported preset: {preset!r}")
    if name == "adaptive_30d":
        return name, None  # weights filled after DB connect
    return name, dict(COMPOSITE_PRESETS[name])


def recompute_composite(
    row: dict[str, Any],
    weights_pct: dict[str, float],
) -> float | None:
    """Recompute composite from row fields using percent weights (sum~100)."""
    weights = {k: float(v) / 100.0 for k, v in weights_pct.items()}
    components: list[tuple[float, float]] = []

    iv_rank = row.get("iv_rank_1y")
    if iv_rank is not None:
        components.append((float(iv_rank), weights.get("iv_rank", 0.0)))

    vrp = row.get("vrp_pct_252d")
    if vrp is not None:
        components.append((float(vrp), weights.get("vrp", 0.0)))

    atm_norm = normalize_atm_slope_score(row.get("atm_slope_30d"))
    if atm_norm is not None:
        components.append((atm_norm, weights.get("atm_slope", 0.0)))

    pin_norm = normalize_pin_score(row.get("pin_pct_distance"))
    if pin_norm is not None:
        components.append((pin_norm, weights.get("pin", 0.0)))

    pin_score = row.get("pin_score")
    terrain_score = float(pin_score) if pin_score is not None else 50.0
    components.append((terrain_score, weights.get("terrain", 0.0)))

    if not components:
        return None
    total_weight = sum(w for _, w in components)
    if total_weight <= 0:
        return None
    return sum(score * weight for score, weight in components) / total_weight


def _latest_scan_date(conn: Any, as_of: date | None) -> date | None:
    with conn.cursor() as cur:
        if as_of is not None:
            cur.execute(
                f"""
                SELECT MAX(trade_date)
                FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY}
                WHERE trade_date <= %s
                """,
                (as_of,),
            )
        else:
            cur.execute(f"SELECT MAX(trade_date) FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY}")
        row = cur.fetchone()
    if not row:
        return None
    value = row[0] if not isinstance(row, dict) else row.get("max")
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _scan_universe_size(conn: Any, trade_date: date) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY}
            WHERE trade_date = %s
            """,
            (trade_date,),
        )
        row = cur.fetchone()
    if not row:
        return 0
    return int(row[0] if not isinstance(row, dict) else row.get("count") or 0)


@router.get("")
def list_scan(
    symbols: str | None = Query(None, description="Comma-separated symbols"),
    as_of: date | None = Query(None, description="Trade date (defaults to latest)"),
    sort_by: str = Query("composite_score"),
    min_composite: float | None = Query(None),
    flag_filter: str | None = Query(None, description="AND filter, e.g. iv_rank:hot,vrp:hot"),
    iv_rank: str | None = Query(None, description="Lens flag: hot|cold|neutral|all"),
    vrp: str | None = Query(None),
    atm_slope: str | None = Query(None),
    pin: str | None = Query(None),
    terrain: str | None = Query(None),
    preset: str = Query("neutral", description="neutral|momentum|mean_revert|adaptive_30d"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort_dir: Literal["asc", "desc"] = Query("desc"),
) -> dict[str, Any]:
    try:
        sort_column = resolve_sort_column(sort_by)
        flag_pairs = merge_flag_filters(
            flag_filter,
            iv_rank=iv_rank,
            vrp=vrp,
            atm_slope=atm_slope,
            pin=pin,
            terrain=terrain,
        )
        preset_name, weights = resolve_preset(preset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    conn = _connect_or_503()
    try:
        if weights is None and preset_name == "adaptive_30d":
            weights = load_adaptive_weights(conn, window_days=30)
        if weights is None:
            weights = dict(COMPOSITE_PRESETS["neutral"])
        trade_date = _latest_scan_date(conn, as_of)
        if trade_date is None:
            return _ok(
                {
                    "as_of": None,
                    "count": 0,
                    "rows": [],
                    "universe_size": 0,
                    "preset": preset_name,
                    "weights": weights,
                }
            )

        where = ["trade_date = %s"]
        params: list[Any] = [trade_date]

        if symbols and symbols.strip():
            sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
            if sym_list:
                where.append("symbol = ANY(%s)")
                params.append(sym_list)

        for key, value in flag_pairs:
            where.append("lens_flags->>%s = %s")
            params.extend([key, value])

        # Fetch a wider set when re-scoring / filtering by recomputed composite
        fetch_limit = max(limit + offset, 500) if preset_name != "neutral" or min_composite is not None else limit + offset

        sql = f"""
            SELECT trade_date, symbol, close, iv_rank_1y, vrp_pct_252d,
                   atm_slope_30d, pin_pct_distance, dte_to_opex,
                   zero_gamma_offset, gex_notional, terrain_regime,
                   pin_score, tail_risk, trend_release, composite_score,
                   lens_flags, computed_at, fetched_at
            FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY}
            WHERE {' AND '.join(where)}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        universe_size = _scan_universe_size(conn, trade_date)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("scan list failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for row in rows:
        td = row.get("trade_date")
        if isinstance(td, date):
            row["trade_date"] = td.isoformat()
        if preset_name != "neutral":
            row["composite_score"] = recompute_composite(row, weights)
            row["composite_source"] = preset_name
        else:
            row["composite_source"] = "stored"

    if min_composite is not None:
        rows = [
            r
            for r in rows
            if r.get("composite_score") is not None and float(r["composite_score"]) >= min_composite
        ]

    reverse = sort_dir == "desc"
    if sort_column == "symbol":
        rows.sort(key=lambda r: str(r.get("symbol") or ""), reverse=reverse)
    else:

        def _sort_key(r: dict[str, Any]) -> tuple[int, float, str]:
            val = r.get(sort_column)
            if val is None:
                return (1, 0.0, str(r.get("symbol") or ""))
            try:
                return (0, float(val), str(r.get("symbol") or ""))
            except (TypeError, ValueError):
                return (1, 0.0, str(r.get("symbol") or ""))

        rows.sort(key=_sort_key, reverse=reverse)

    # Apply pagination after re-score / filter
    page = rows[offset : offset + limit]
    _ = fetch_limit  # retained for clarity / future SQL pushdown

    return _ok(
        {
            "as_of": trade_date.isoformat(),
            "count": len(page),
            "rows": page,
            "universe_size": universe_size,
            "preset": preset_name,
            "weights": weights,
        }
    )


__all__ = [
    "COMPOSITE_PRESETS",
    "FLAG_KEYS",
    "FLAG_VALUES",
    "SORT_COLUMNS",
    "merge_flag_filters",
    "parse_flag_filter",
    "recompute_composite",
    "resolve_preset",
    "resolve_sort_column",
    "router",
]
