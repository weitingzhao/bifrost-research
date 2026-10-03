"""Read-only helpers for the SEPA tier marts ``dw_stock.mart_sepa_tier_{momentum,structure,sentiment}``.

Each mart holds one row per symbol for its latest ``eval_date``: boolean signal
columns and ``<tier>_score``, the passing fraction (signals passed / signals).
The signal ids below ARE those column names (dbt models in ``dbt/models/marts``).

Moved here from Trade API ``routers/data_readiness`` (TD-49 step 2, 0.157.0) so
the marts' owner serves them; the answers keep the shapes Trade API returned so
its routes become a proxy.
"""

from __future__ import annotations

from typing import Any, Dict, List

from psycopg2.extras import RealDictCursor

from bifrost_research.db.conn import get_conn

TIER_COLUMNS: Dict[str, tuple[str, ...]] = {
    "momentum": (
        "rsi_above_50",
        "rsi_healthy_range",
        "macd_bullish",
        "macd_strong",
        "roc_10_positive",
        "roc_21_positive",
        "rs_gt_spy",
        "volume_expanding",
        "volume_surge",
        "price_gt_sma10",
    ),
    "structure": (
        "bb_squeeze",
        "bb_tight_squeeze",
        "adx_trending",
        "adx_strong_trend",
        "aroon_bullish",
        "aroon_up_strong",
        "vol_contracting",
        "vol_tight_contraction",
    ),
    "sentiment": (
        "si_declining",
        "low_short_float",
        "high_days_to_cover",
        "short_float_declining",
        "low_short_volume",
        "sv_ratio_declining",
    ),
}
# The score is a fraction; filters and histograms speak in signals passed (0..N).
TIER_MAX_SCORE: Dict[str, int] = {k: len(v) for k, v in TIER_COLUMNS.items()}


def _table(tier: str) -> str:
    if tier not in TIER_COLUMNS:
        raise ValueError(f"unknown tier: {tier}")
    return f"dw_stock.mart_sepa_tier_{tier}"


def _passed_sql(tier: str) -> str:
    """Signals passed, as an integer 0..N, from the stored fraction."""
    return f"round({tier}_score * {TIER_MAX_SCORE[tier]})::int"


def fetch_tier_stats(tier: str) -> Dict[str, Any]:
    """Per-signal pass counts and the signals-passed histogram for one tier, latest eval_date."""
    table = _table(tier)
    cols = TIER_COLUMNS[tier]
    per_signal = ", ".join(f"count(*) FILTER (WHERE {c} IS TRUE) AS {c}" for c in cols)
    latest = f"eval_date = (SELECT max(eval_date) FROM {table})"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT max(eval_date) AS d, count(*) AS n, {per_signal} FROM {table} WHERE {latest}")
            head = cur.fetchone() or {}
            cur.execute(f"SELECT {_passed_sql(tier)} AS s, count(*) AS c FROM {table} WHERE {latest} GROUP BY 1")
            hist_rows = cur.fetchall() or []
    hist = {i: 0 for i in range(TIER_MAX_SCORE[tier] + 1)}
    for r in hist_rows:
        hist[int(r["s"] or 0)] = int(r["c"] or 0)
    return {
        "ok": True,
        "tier": tier,
        "eval_date": str(head["d"]) if head.get("d") else None,
        "universe_count": int(head.get("n") or 0),
        "max_score": TIER_MAX_SCORE[tier],
        "conditions": [{"id": c, "pass": int(head.get(c) or 0)} for c in cols],
        "pass_count_distribution": hist,
        # The vocabulary, from the marts' owner: a client need not carry its own copy.
        "signals": list(cols),
    }


def fetch_tier_filter(tier: str, cond_ids: List[str], min_score: int, match: str, limit: int) -> Dict[str, Any]:
    """Names on the latest eval_date passing the picked signals (all / any) and at least min_score of them.

    ``cond_ids`` must already be valid column names for ``tier`` (the route checks).
    """
    table = _table(tier)
    valid = set(TIER_COLUMNS[tier])
    bad = [c for c in cond_ids if c not in valid]
    if bad:
        raise ValueError(f"unknown {tier} signal ids: {', '.join(bad)}")
    passed = _passed_sql(tier)
    where = [f"eval_date = (SELECT max(eval_date) FROM {table})"]
    if cond_ids:
        joiner = " OR " if match == "any" else " AND "
        where.append("(" + joiner.join(f"{c} IS TRUE" for c in cond_ids) + ")")
    if min_score > 0:
        where.append(f"{passed} >= %s")
    sql = (
        f"SELECT symbol, {passed} AS score, eval_date, count(*) OVER () AS total "
        f"FROM {table} WHERE {' AND '.join(where)} "
        f"ORDER BY {tier}_score DESC, symbol ASC LIMIT %s"
    )
    params: List[Any] = ([min_score] if min_score > 0 else []) + [limit]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() or []
    total = int(rows[0]["total"]) if rows else 0
    return {
        "ok": True,
        "tier": tier,
        "include": cond_ids,
        "match": match,
        "min_score": min_score,
        "max_score": TIER_MAX_SCORE[tier],
        # The whole match, not the page: a limited list is a floor, never a count.
        "count": total,
        "truncated": total > len(rows),
        "eval_date": str(rows[0]["eval_date"]) if rows else None,
        "symbols": [{"symbol": r["symbol"], "score": int(r["score"] or 0)} for r in rows],
        "limit": limit,
    }
