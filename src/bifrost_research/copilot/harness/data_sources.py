"""Harness data source adapter — Wave Y.1 Loop Smartness.

Thin, in-process helpers that let the harness runtime read `features.*` scan and
signal-decay tables directly.  Reuses Scan API pure functions (`resolve_preset`,
`recompute_composite`) — never HTTP — so harness and `/research/scan` share
one weight table.

D10 BLOCKED — read-only.  All functions must fail soft (return empty results) so
the runtime can fall back to seed_symbols per spine `D-Loop-Smartness-Y1` decision B1.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Protocol

from bifrost_research.db.conn import rollback_quietly
from bifrost_research.api.scan import (
    load_adaptive_weights,
    recompute_composite,
    resolve_preset,
)
from bifrost_research.schema.schemas import (
    TABLE_STOCK_SIGNAL_LENS_HIT_DAILY,
    TABLE_STOCK_SIGNAL_SCAN_DAILY,
)

logger = logging.getLogger(__name__)

VALID_FLAG_KEYS = frozenset({"iv_rank", "vrp", "atm_slope", "pin", "terrain"})
VALID_FLAG_VALUES = frozenset({"hot", "cold", "neutral"})
VALID_LENSES = frozenset({"iv_rank", "vrp", "opex_pin"})

# Flag-filter keys → `features.stock_signal_lens_hit_daily.lens`.
# Unmapped keys (atm_slope, terrain) are skipped by the hit-rate gate.
FLAG_TO_DECAY_LENS: dict[str, str] = {
    "iv_rank": "iv_rank",
    "vrp": "vrp",
    "pin": "opex_pin",
    "opex_pin": "opex_pin",
}

_SCAN_COLUMNS = (
    "trade_date",
    "symbol",
    "close",
    "iv_rank_1y",
    "vrp_pct_252d",
    "atm_slope_30d",
    "pin_pct_distance",
    "composite_score",
    "lens_flags",
    "terrain_regime",
    "pin_score",
)

# The columns that actually feed `composite_score`.  A row with none of them
# still gets a score — the neutral 50 — and a neutral 50 outranks every honestly
# computed score below it.  Measured 2026-09-01 over the full scan table: all 318
# zero-input rows carry composite_score = 50 exactly, and on 2026-08-31 such a row
# (SPX, 0/8 inputs) ranked second out of 26.  `close` is deliberately excluded —
# it is a price field, not a scoring input, and gating on it would empty 20 of 61
# trading days rather than 2.
_SCAN_INPUT_COLUMNS = (
    "iv_rank_1y",
    "vrp_pct_252d",
    "atm_slope_30d",
    "pin_pct_distance",
    "gex_notional",
    "pin_score",
    "tail_risk",
    "trend_release",
)

# Default floor: a row must carry at least one real scoring input to be ranked.
DEFAULT_MIN_SCAN_INPUTS = 1

_PRESET_FETCH_LIMIT = 500


def _scan_inputs_expr() -> str:
    """SQL counting how many scoring inputs a scan row actually has."""
    return " + ".join(f"({col} IS NOT NULL)::int" for col in _SCAN_INPUT_COLUMNS)


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def latest_scan_trade_date(conn: _Connection) -> date | None:
    """Most recent ``trade_date`` in ``features.stock_signal_scan_daily``."""
    sql = f"SELECT MAX(trade_date) FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY}"
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            val = row[0]
            if isinstance(val, date):
                return val
            return date.fromisoformat(str(val)[:10])
    except Exception as exc:  # noqa: BLE001
        rollback_quietly(conn)
        logger.warning("latest_scan_trade_date failed: %s", exc)
        return None


def scan_stale_days(conn: _Connection, *, today: date | None = None) -> int | None:
    """Calendar days since latest scan snapshot; None when table empty."""
    latest = latest_scan_trade_date(conn)
    if latest is None:
        return None
    ref = today or date.today()
    return max(0, (ref - latest).days)


def parse_flag_filter(raw: str | None) -> list[tuple[str, str]]:
    """Parse ``iv_rank:hot,vrp:hot`` into ANDed (key, value) pairs.

    Returns [] for empty / None input.  Raises ValueError for malformed segments so
    the runtime can decide whether to fail the objective or fall back.
    """
    if not raw or not str(raw).strip():
        return []
    pairs: list[tuple[str, str]] = []
    for segment in str(raw).split(","):
        part = segment.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"invalid flag filter segment: {part!r}")
        key, value = part.split(":", 1)
        key = key.strip()
        value = value.strip().lower()
        if key not in VALID_FLAG_KEYS:
            raise ValueError(f"unknown flag key: {key!r}")
        if value not in VALID_FLAG_VALUES:
            raise ValueError(f"unknown flag value: {value!r}")
        pairs.append((key, value))
    return pairs


def _resolve_preset_weights(
    conn: _Connection, preset: str | None
) -> tuple[str, dict[str, float] | None]:
    """Return ``(preset_name, weights)``. ``weights is None`` → use stored scores."""
    raw = (preset or "neutral").strip().lower() or "neutral"
    try:
        name, weights = resolve_preset(raw)
    except ValueError:
        logger.warning("top_scan_symbols: unsupported preset %r; using stored scores", preset)
        return "neutral", None
    if name == "neutral":
        return name, None
    if weights is None and name == "adaptive_30d":
        try:
            weights = load_adaptive_weights(conn, window_days=30)
        except Exception as exc:  # noqa: BLE001
            rollback_quietly(conn)
            logger.warning(
                "top_scan_symbols: adaptive weights failed (%s); using stored scores",
                exc,
            )
            return "neutral", None
    if not weights:
        return "neutral", None
    return name, weights


def top_scan_symbols(
    conn: _Connection,
    *,
    limit: int = 5,
    flag_filter: str | None = None,
    min_composite_score: float | None = None,
    as_of: date | None = None,
    preset: str | None = None,
    min_inputs: int = DEFAULT_MIN_SCAN_INPUTS,
) -> list[dict[str, Any]]:
    """Return top-N symbols from the most recent scan snapshot.

    Ordered by composite_score DESC (stored, or recomputed via Scan
    ``resolve_preset`` / ``recompute_composite`` when preset is not
    ``neutral``).  Applies AND semantics for flag_filter
    (``lens_flags->>'iv_rank' = 'hot'``).

    Fails soft: on any DB error / empty table returns an empty list so the
    runtime can fall back to seed_symbols.
    """
    limit = max(1, min(int(limit or 5), 50))
    preset_name, weights = _resolve_preset_weights(conn, preset)
    needs_rescore = weights is not None

    try:
        pairs = parse_flag_filter(flag_filter)
    except ValueError:
        logger.warning("top_scan_symbols: invalid flag_filter %r; ignoring", flag_filter)
        pairs = []

    where: list[str] = []
    params: list[Any] = []

    where.append(
        "trade_date = ("
        f"SELECT MAX(trade_date) FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY} "
        + ("WHERE trade_date <= %s" if as_of else "")
        + ")"
    )
    if as_of is not None:
        params.append(as_of)

    if min_inputs > 0:
        where.append(f"({_scan_inputs_expr()}) >= {int(min_inputs)}")

    for key, value in pairs:
        where.append("lens_flags ->> %s = %s")
        params.extend([key, value])

    if min_composite_score is not None and not needs_rescore:
        where.append("composite_score IS NOT NULL AND composite_score >= %s")
        params.append(float(min_composite_score))

    fetch_limit = _PRESET_FETCH_LIMIT if needs_rescore else limit
    sql = (
        f"SELECT {', '.join(_SCAN_COLUMNS)} "
        f"FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY} "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY composite_score DESC NULLS LAST, symbol ASC "
        f"LIMIT {fetch_limit}"
    )

    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall() or []]
    except Exception as exc:  # noqa: BLE001
        rollback_quietly(conn)
        logger.warning("top_scan_symbols: query failed (%s); returning empty", exc)
        return []

    if needs_rescore and weights is not None:
        for row in rows:
            row["composite_score"] = recompute_composite(row, weights)
            row["composite_source"] = preset_name
        if min_composite_score is not None:
            floor = float(min_composite_score)
            rows = [
                r
                for r in rows
                if r.get("composite_score") is not None
                and float(r["composite_score"]) >= floor
            ]
        rows.sort(
            key=lambda r: (
                r.get("composite_score") is None,
                -(float(r["composite_score"]) if r.get("composite_score") is not None else 0.0),
                str(r.get("symbol") or ""),
            )
        )
        rows = rows[:limit]
    else:
        for row in rows:
            row.setdefault("composite_source", "stored")

    return rows


def scan_universe_funnel(
    conn: _Connection,
    *,
    flag_filter: str | None = None,
    min_composite_score: float | None = None,
    as_of: date | None = None,
    min_inputs: int = DEFAULT_MIN_SCAN_INPUTS,
) -> dict[str, Any]:
    """Stage-by-stage counts for the scan universe on one trade date.

    ``top_scan_symbols`` queries with ``LIMIT``, so the rows it returns cannot
    say how many symbols were considered — a funnel built from them reports
    ``3 -> 3`` no matter how large the universe was.  This counts the same
    predicates over the whole day instead, so the harness can report
    ``28 considered -> 3 proposed``.

    Mirrors ``top_scan_symbols`` filter-for-filter; keep the two in step.
    Fails soft: returns zeroed counts on any DB error.
    """
    empty = {
        "trade_date": None,
        "total": 0,
        "with_inputs": 0,
        "flag_passed": 0,
        "score_passed": 0,
    }
    try:
        pairs = parse_flag_filter(flag_filter)
    except ValueError:
        logger.warning("scan_universe_funnel: invalid flag_filter %r; ignoring", flag_filter)
        pairs = []

    inputs_pred = f"({_scan_inputs_expr()}) >= {int(min_inputs)}" if min_inputs > 0 else "TRUE"
    flag_pred = " AND ".join("lens_flags ->> %s = %s" for _ in pairs) if pairs else "TRUE"
    score_pred = (
        "composite_score IS NOT NULL AND composite_score >= %s"
        if min_composite_score is not None
        else "TRUE"
    )
    day_pred = (
        "trade_date = ("
        f"SELECT MAX(trade_date) FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY} "
        + ("WHERE trade_date <= %s" if as_of else "")
        + ")"
    )

    # Placeholders bind in SQL text order: the SELECT list runs before WHERE, and
    # flag_pred appears in two FILTER clauses, so its params are supplied twice.
    flag_params: list[Any] = []
    for key, value in pairs:
        flag_params.extend([key, value])
    params: list[Any] = [*flag_params, *flag_params]
    if min_composite_score is not None:
        params.append(float(min_composite_score))
    if as_of is not None:
        params.append(as_of)

    sql = (
        "SELECT MAX(trade_date) AS trade_date, "
        "count(*) AS total, "
        f"count(*) FILTER (WHERE {inputs_pred}) AS with_inputs, "
        f"count(*) FILTER (WHERE {inputs_pred} AND ({flag_pred})) AS flag_passed, "
        f"count(*) FILTER (WHERE {inputs_pred} AND ({flag_pred}) AND ({score_pred})) AS score_passed "
        f"FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY} "
        f"WHERE {day_pred}"
    )

    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        rollback_quietly(conn)
        logger.warning("scan_universe_funnel: query failed (%s); returning zeros", exc)
        return empty
    if not row:
        return empty
    return {
        "trade_date": row[0],
        "total": int(row[1] or 0),
        "with_inputs": int(row[2] or 0),
        "flag_passed": int(row[3] or 0),
        "score_passed": int(row[4] or 0),
    }


def global_signal_decay_summary(
    conn: _Connection,
    *,
    window_days: int = 30,
    lenses: tuple[str, ...] = ("iv_rank", "vrp", "opex_pin"),
) -> dict[str, dict[str, Any]]:
    """Return global hit-rate rollup keyed by lens.

    Shape::

        {
          "iv_rank": {"n": 42, "hit_rate_5d": 0.55, "hit_rate_20d": 0.62},
          "vrp":     {"n": 30, "hit_rate_5d": 0.48, "hit_rate_20d": None},
          ...
        }

    Missing lens → empty dict entry with n=0.  Used for trace + Y.3 must_pass
    gating; NOT per-symbol filtering (that comes in Y.3).

    Fails soft: on any DB error returns entries with n=0.
    """
    window_days = max(1, min(int(window_days or 30), 400))
    normalized = tuple(l for l in lenses if l in VALID_LENSES) or ("iv_rank",)

    result: dict[str, dict[str, Any]] = {
        lens: {"n": 0, "hit_rate_5d": None, "hit_rate_20d": None} for lens in normalized
    }

    sql = f"""
        SELECT lens,
               COUNT(*)                                                    AS n,
               COUNT(*) FILTER (WHERE hit_5d IS NOT NULL)                  AS eval5,
               COUNT(*) FILTER (WHERE hit_5d IS TRUE)                      AS ok5,
               COUNT(*) FILTER (WHERE hit_20d IS NOT NULL)                 AS eval20,
               COUNT(*) FILTER (WHERE hit_20d IS TRUE)                     AS ok20
        FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY}
        WHERE lens = ANY(%s)
          AND trade_date >= CURRENT_DATE - %s::int
        GROUP BY lens
    """

    try:
        with conn.cursor() as cur:
            cur.execute(sql, (list(normalized), window_days + 5))
            for lens, n, eval5, ok5, eval20, ok20 in cur.fetchall() or []:
                if lens not in result:
                    continue
                result[lens] = {
                    "n": int(n or 0),
                    "hit_rate_5d": round(float(ok5) / eval5, 4) if eval5 else None,
                    "hit_rate_20d": round(float(ok20) / eval20, 4) if eval20 else None,
                }
    except Exception as exc:  # noqa: BLE001
        rollback_quietly(conn)
        logger.warning("global_signal_decay_summary: query failed (%s); returning empty", exc)

    return result
