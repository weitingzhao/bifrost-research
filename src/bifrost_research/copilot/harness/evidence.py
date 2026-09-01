"""Per-candidate evidence — why this symbol, and what would make it wrong.

A proposal with a score and no reasoning is an opinion. This assembles the four
things that turn one into a recommendation someone can act on or reject:

  selection        which signal picked it, and how far above the bar it sits
  price_context    where the price is against its own trend
  option_analytics the single-stock option view — absent for most symbols
  track_record     how candidates from this source have actually done (Wave W2)
  invalidation     the specific conditions that would flip the call

`option_analytics` is NOT MEASURED for most candidates on purpose. Terrain, GEX
and IV percentile are option-derived and cover only the ~28 symbols with option
data; of the eight the stock funnel proposed on 2026-09-01, one had them. Saying
so is the point — a blank panel would read as "nothing notable", which is a
claim about the stock rather than about our coverage.

Read-only. D13: reads ``features.*`` and ``research.*``, writes nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from bifrost_research.db.conn import rollback_quietly
from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_CANDIDATE_OUTCOME,
    TABLE_RESEARCH_CANDIDATE_POOL,
    TABLE_STOCK_SIGNAL_SEPA_DAILY,
)

logger = logging.getLogger(__name__)

NOT_MEASURED = "not_measured"

_SEPA_COLUMNS = (
    "sepa_score",
    "grade",
    "stage",
    "path",
    "fundamental_score",
    "trend_template_score",
    "momentum_score",
    "structure_score",
    "latest_close",
    "sma_50",
    "sma_200",
    "high_52w",
    "low_52w",
    "fund_pass_count",
    "tech_pass_count",
)


def _fetch_sepa(conn: Any, symbol: str) -> dict[str, Any] | None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_SEPA_COLUMNS)}
                FROM {TABLE_STOCK_SIGNAL_SEPA_DAILY}
                WHERE symbol = %s
                  AND trade_date = (SELECT MAX(trade_date) FROM {TABLE_STOCK_SIGNAL_SEPA_DAILY})
                """,
                (symbol.upper(),),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("evidence: sepa lookup failed for %s (%s)", symbol, exc)
        rollback_quietly(conn)
        return None
    if not row:
        return None
    return {col: row[i] for i, col in enumerate(_SEPA_COLUMNS)}


def _fetch_option_analytics(conn: Any, symbol: str) -> dict[str, Any]:
    """IV percentile / GEX / terrain for one symbol, or why they are absent."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  (SELECT iv_rank_1y FROM features.option_metric_iv_percentile_daily
                    WHERE symbol = %s ORDER BY trade_date DESC LIMIT 1),
                  (SELECT total_net_gex FROM features.option_metric_gex_levels_daily
                    WHERE symbol = %s ORDER BY trade_date DESC LIMIT 1),
                  (SELECT regime FROM features.stock_forecast_terrain_daily
                    WHERE symbol = %s ORDER BY trade_date DESC LIMIT 1)
                """,
                (symbol.upper(), symbol.upper(), symbol.upper()),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("evidence: option analytics failed for %s (%s)", symbol, exc)
        rollback_quietly(conn)
        return {"status": NOT_MEASURED, "reason": f"query failed: {exc}"}

    try:
        iv_rank, net_gex, regime = row
    except (TypeError, ValueError):
        # A driver that hands back something other than the three columns asked
        # for is a fact about our reading, not about the symbol.
        return {"status": NOT_MEASURED, "reason": "option analytics row was unreadable"}
    if iv_rank is None and net_gex is None and regime is None:
        return {
            "status": NOT_MEASURED,
            "reason": (
                "no option analytics for this symbol — terrain / GEX / IV percentile "
                "are derived from option data, which covers a small subset of the "
                "stock universe"
            ),
        }
    return {
        "status": "ok",
        "iv_rank_1y": float(iv_rank) if iv_rank is not None else None,
        "total_net_gex": float(net_gex) if net_gex is not None else None,
        "terrain_regime": regime,
    }


def _fetch_track_record(conn: Any, source: str | None) -> dict[str, Any]:
    """How candidates from this source have actually settled (Wave W2)."""
    if not source:
        return {"status": NOT_MEASURED, "reason": "candidate has no source"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT o.horizon_days,
                       count(*) FILTER (WHERE o.hit IS NOT NULL) AS judged,
                       count(*) FILTER (WHERE o.hit) AS hits,
                       avg(o.excess_return) AS avg_excess
                FROM {TABLE_RESEARCH_CANDIDATE_OUTCOME} o
                JOIN {TABLE_RESEARCH_CANDIDATE_POOL} c ON c.id = o.candidate_id
                WHERE c.source = %s
                GROUP BY o.horizon_days
                ORDER BY o.horizon_days
                """,
                (source,),
            )
            rows = cur.fetchall() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("evidence: track record failed for %s (%s)", source, exc)
        rollback_quietly(conn)
        return {"status": NOT_MEASURED, "reason": f"query failed: {exc}"}

    horizons = [
        {
            "horizon_days": int(h),
            "judged": int(judged or 0),
            "hit_rate": (float(hits) / float(judged)) if judged else None,
            "avg_excess": float(avg) if avg is not None else None,
        }
        for h, judged, hits, avg in rows
    ]
    if not any(h["judged"] for h in horizons):
        return {
            "status": NOT_MEASURED,
            "reason": (
                "no candidate from this source has reached a settled horizon yet — "
                "the ledger fills as proposals age, it is not a zero hit rate"
            ),
            "horizons": horizons,
        }
    return {"status": "ok", "source": source, "horizons": horizons}


def _invalidation(sepa: dict[str, Any] | None, *, min_score: float | None) -> list[str]:
    """The specific conditions under which this pick stops being one."""
    out: list[str] = []
    if sepa is None:
        return ["no SEPA row — selection rationale cannot be restated"]
    score = sepa.get("sepa_score")
    if min_score is not None and score is not None:
        out.append(f"sepa_score falls below {min_score:g} (now {float(score):.1f})")
    path = sepa.get("path")
    if path:
        out.append(f"path leaves {path} — the setup it was selected on no longer holds")
    close, sma_50 = sepa.get("latest_close"), sepa.get("sma_50")
    if close is not None and sma_50 is not None:
        out.append(f"close breaks below the 50-day ({float(sma_50):.2f})")
    return out


def build_candidate_evidence(
    conn: Any,
    symbol: str,
    *,
    source: str | None = None,
    min_score: float | None = None,
) -> dict[str, Any]:
    """Assemble the evidence chain for one proposed symbol."""
    sym = str(symbol or "").strip().upper()
    sepa = _fetch_sepa(conn, sym)

    selection: dict[str, Any]
    price_context: dict[str, Any]
    if sepa is None:
        reason = "no SEPA row for this symbol on the latest trade date"
        selection = {"status": NOT_MEASURED, "reason": reason}
        price_context = {"status": NOT_MEASURED, "reason": reason}
    else:
        selection = {
            "status": "ok",
            "sepa_score": _f(sepa.get("sepa_score")),
            "grade": sepa.get("grade"),
            "stage": sepa.get("stage"),
            "path": sepa.get("path"),
            "components": {
                "fundamental": _f(sepa.get("fundamental_score")),
                "trend_template": _f(sepa.get("trend_template_score")),
                "momentum": _f(sepa.get("momentum_score")),
                "structure": _f(sepa.get("structure_score")),
            },
            "checks_passed": {
                "fundamental": sepa.get("fund_pass_count"),
                "technical": sepa.get("tech_pass_count"),
            },
        }
        price_context = {
            "status": "ok",
            "close": _f(sepa.get("latest_close")),
            "sma_50": _f(sepa.get("sma_50")),
            "sma_200": _f(sepa.get("sma_200")),
            "high_52w": _f(sepa.get("high_52w")),
            "low_52w": _f(sepa.get("low_52w")),
            "pct_off_52w_high": _pct_off_high(sepa),
        }

    return {
        "symbol": sym,
        "selection": selection,
        "price_context": price_context,
        "option_analytics": _fetch_option_analytics(conn, sym),
        "track_record": _fetch_track_record(conn, source),
        "invalidation": _invalidation(sepa, min_score=min_score),
    }


def _f(value: Any) -> float | None:
    return float(value) if value is not None else None


def _pct_off_high(sepa: dict[str, Any]) -> float | None:
    close, high = sepa.get("latest_close"), sepa.get("high_52w")
    if close is None or high is None or float(high) <= 0:
        return None
    return round((float(close) / float(high) - 1.0) * 100.0, 2)
