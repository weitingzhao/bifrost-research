"""Exhibit readers for the lenses Wave 15 did not cover, and the enrichment every
exhibit gets (moved from api/ in research-loop-automation C3 so the Daily Brief
engine can read the same exhibits without importing the HTTP layer): verdict from the registry, settled track record, similar-regime summary.

Each reader answers "what is the latest reading and how fresh is it" from the
lens' own table; ``enrich_exhibit`` then attaches the three things that turn a
reading into something a page or Copilot can act on. Failures degrade to a
caveat, never to a 500 — an exhibit with a missing block is still an exhibit.
"""

from __future__ import annotations

import logging
from typing import Any

from bifrost_research.lenses.exhibit_model import ExhibitResponse, freshness_from, iso_date, rollback_quietly
from bifrost_research.api.similar_regime import similar_rows
from bifrost_research.lenses.registry import LENSES
from bifrost_research.lenses.similar import summarize_forward_returns
from bifrost_research.lenses.track_record import fetch_track_record
from bifrost_research.lenses.verdict import verdict_for
from bifrost_research.repositories import opex_cycle as opex_repo
from bifrost_research.schema.schemas import (
    TABLE_OPTION_METRIC_GEX_LEVELS_DAILY,
    TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY,
    TABLE_OPTION_METRIC_MAX_PAIN_DAILY,
    TABLE_STOCK_BACKTEST_SETTLEMENT,
    TABLE_STOCK_SIGNAL_MOMENTUM_DAILY,
    TABLE_STOCK_SIGNAL_SEPA_DAILY,
    TABLE_STOCK_SIGNAL_VRP_DAILY,
)

logger = logging.getLogger(__name__)

SURFACE_FIT = "features.option_surface_fit_daily"
STOCK_DAILY = "raw_market.stock_daily"

SIMILAR_K = 8
SIMILAR_HORIZON = 5
TAPE_SOURCE = "option_trades_tape"


def _fetch_one(conn: Any, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _fetch_all(conn: Any, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall() or [])


def _failed(exh: ExhibitResponse, what: str, exc: Exception, conn: Any) -> ExhibitResponse:
    exh.caveats.append(f"{what} query failed: {exc}")
    rollback_quietly(conn)
    return exh


# ─── readers ────────────────────────────────────────────────────────────────


def exhibit_skew(conn: Any, symbol: str) -> ExhibitResponse:
    exh = ExhibitResponse(lens="skew", symbol=symbol)
    try:
        row = _fetch_one(
            conn,
            f"""
            SELECT trade_date, expiry, dte, atm_vol, atm_slope, fit_rmse, n_points, computed_at
            FROM {SURFACE_FIT}
            WHERE symbol = %s
              AND trade_date = (SELECT MAX(trade_date) FROM {SURFACE_FIT} WHERE symbol = %s)
            ORDER BY ABS(dte - 30) ASC, expiry ASC
            LIMIT 1
            """,
            (symbol, symbol),
        )
        today_abs = abs(float(row[4])) if row and row[4] is not None else None
        # C2: the reading is judged against the symbol's own year, not a fixed
        # slope. The percentile is the share of history days whose |slope| sat
        # below today's — one near-30-DTE fit per day, today excluded.
        hist = _fetch_one(
            conn,
            f"""
            SELECT COUNT(*)::bigint,
                   AVG(a),
                   100.0 * COUNT(*) FILTER (WHERE a < %s) / NULLIF(COUNT(*), 0)
            FROM (
                SELECT DISTINCT ON (trade_date) trade_date, ABS(atm_slope) AS a
                FROM {SURFACE_FIT}
                WHERE symbol = %s AND atm_slope IS NOT NULL
                  AND trade_date < %s
                  AND trade_date >= %s::date - INTERVAL '252 days'
                ORDER BY trade_date, ABS(dte - 30) ASC, expiry ASC
            ) s
            """,
            (today_abs if today_abs is not None else -1.0, symbol, row[0] if row else None, row[0] if row else None),
        ) if row else None
        if row:
            days = int(hist[0] or 0) if hist else 0
            pctile = float(hist[2]) if hist and hist[2] is not None else None
            exh.as_of = iso_date(row[0])
            exh.readings = {
                "atm_slope": row[4],
                "abs_atm_slope": today_abs,
                "slope_pctile_252d": pctile,
                "history_days": days,
                "atm_vol": row[3],
                "dte": row[2],
                "expiry": iso_date(row[1]),
                "fit_rmse": row[5],
                "n_points": row[6],
            }
            exh.freshness = freshness_from(row[7], True)
            if pctile is None:
                exh.caveats.append("No prior fit days — skew percentile unknown")
            elif days < 60:
                exh.caveats.append(f"Skew percentile rests on {days} history days — thin")
        else:
            exh.caveats.append("No SVI fit rows for symbol")
        if hist:
            exh.history_summary = {"days": int(hist[0] or 0), "avg_abs_atm_slope": hist[1]}
    except Exception as exc:
        return _failed(exh, "Skew", exc, conn)
    return exh


# Term-structure bands on near − far ATM vol, mirrored from the registry's
# term_slope spec (kept in one place there; the label is what the page prints).
BACKWARDATION_MIN = 0.02
STEEP_CONTANGO_MAX = -0.03


def term_structure_label(backwardation: float | None) -> str | None:
    """``backwardation`` / ``contango`` / ``flat`` for near − far ATM vol."""
    if backwardation is None:
        return None
    if backwardation >= BACKWARDATION_MIN:
        return "backwardation"
    if backwardation <= STEEP_CONTANGO_MAX:
        return "contango"
    return "flat"


def exhibit_term_slope(conn: Any, symbol: str) -> ExhibitResponse:
    exh = ExhibitResponse(lens="term_slope", symbol=symbol)
    try:
        rows = _fetch_all(
            conn,
            f"""
            SELECT trade_date, expiry, dte, atm_vol, computed_at
            FROM {SURFACE_FIT}
            WHERE symbol = %s
              AND trade_date = (SELECT MAX(trade_date) FROM {SURFACE_FIT} WHERE symbol = %s)
              AND dte IS NOT NULL AND atm_vol IS NOT NULL
            ORDER BY dte ASC
            """,
            (symbol, symbol),
        )
        if not rows:
            exh.caveats.append("No SVI fit rows for symbol")
            return exh
        near = min(rows, key=lambda r: abs(int(r[2]) - 30))
        far_candidates = [r for r in rows if int(r[2]) > int(near[2])]
        far = min(far_candidates, key=lambda r: abs(int(r[2]) - 90)) if far_candidates else None
        exh.as_of = iso_date(near[0])
        exh.freshness = freshness_from(near[4], True)
        term_slope = (float(far[3]) - float(near[3])) if far else None
        backwardation = (-term_slope) if term_slope is not None else None
        exh.readings = {
            "near_dte": near[2],
            "near_vol": near[3],
            "far_dte": far[2] if far else None,
            "far_vol": far[3] if far else None,
            "term_slope": term_slope,
            # C2: judged on near − far, so backwardation reads positive.
            "backwardation": backwardation,
            "term_structure": term_structure_label(backwardation),
            "expiries_fitted": len(rows),
        }
        if far is None:
            exh.caveats.append("Only one fitted expiry — no term slope")
    except Exception as exc:
        return _failed(exh, "Term slope", exc, conn)
    return exh


def exhibit_gex_regime(conn: Any, symbol: str) -> ExhibitResponse:
    exh = ExhibitResponse(lens="gex_regime", symbol=symbol)
    try:
        row = _fetch_one(
            conn,
            f"""
            SELECT trade_date, expiry, spot, total_net_gex, zero_gamma,
                   major_call_wall, major_put_wall, computed_at
            FROM {TABLE_OPTION_METRIC_GEX_LEVELS_DAILY}
            WHERE symbol = %s
              AND trade_date = (
                  SELECT MAX(trade_date) FROM {TABLE_OPTION_METRIC_GEX_LEVELS_DAILY} WHERE symbol = %s
              )
            ORDER BY ABS((expiry - trade_date) - 30) ASC, expiry ASC
            LIMIT 1
            """,
            (symbol, symbol),
        )
        if not row:
            exh.caveats.append("No GEX level rows for symbol")
            return exh
        net = row[3]
        spot, zero_gamma = row[2], row[4]
        regime = None if net is None else ("negative" if float(net) < 0 else "positive")
        vs_zero = None
        if spot is not None and zero_gamma is not None:
            vs_zero = "above" if float(spot) >= float(zero_gamma) else "below"
        exh.as_of = iso_date(row[0])
        exh.freshness = freshness_from(row[7], True)
        exh.readings = {
            "regime": regime,
            "total_net_gex": net,
            "spot": spot,
            "zero_gamma": zero_gamma,
            "spot_vs_zero_gamma": vs_zero,
            "major_call_wall": row[5],
            "major_put_wall": row[6],
            "expiry": iso_date(row[1]),
        }
        # C2: the regime's claim is about realised vol, so the exhibit carries
        # the realised-vs-implied reading it implies — positive gamma should
        # keep RV under IV, negative gamma lets it run over.
        vrp = _fetch_one(
            conn,
            f"""
            SELECT trade_date, vrp_pct_252d, rv_20d, atm_iv_30d, vrp_20d
            FROM {TABLE_STOCK_SIGNAL_VRP_DAILY}
            WHERE symbol = %s AND atm_iv_30d IS NOT NULL
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (symbol,),
        )
        if vrp:
            exh.readings["vrp_link"] = {
                "as_of": iso_date(vrp[0]),
                "vrp_pct_252d": vrp[1],
                "rv_20d": vrp[2],
                "atm_iv_30d": vrp[3],
                "vrp_20d": vrp[4],
                "consistent": (
                    None
                    if regime is None or vrp[2] is None or vrp[3] is None
                    else (float(vrp[2]) <= float(vrp[3])) == (regime == "positive")
                ),
            }
        else:
            exh.caveats.append("No VRP row — the realised-vol side of the regime is unmeasured")
    except Exception as exc:
        return _failed(exh, "GEX regime", exc, conn)
    return exh


PIN_CYCLES = 24
PINNED_WITHIN = 0.005


def pin_history(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Share of settled OpEx cycles that closed within 0.5% of max pain."""
    dists = [abs(float(r["pct_distance"])) for r in rows if isinstance(r.get("pct_distance"), (int, float))]
    if not dists:
        return {"cycles": 0, "pin_rate": None, "pinned_within": PINNED_WITHIN}
    pinned = sum(1 for d in dists if d < PINNED_WITHIN)
    return {
        "cycles": len(dists),
        "pinned": pinned,
        "pin_rate": pinned / len(dists),
        "pinned_within": PINNED_WITHIN,
        "median_abs_distance": sorted(dists)[len(dists) // 2],
    }


def exhibit_opex_pin(conn: Any, symbol: str) -> ExhibitResponse:
    exh = ExhibitResponse(lens="opex_pin", symbol=symbol)
    try:
        row = _fetch_one(
            conn,
            f"""
            SELECT trade_date, expiry, max_pain_strike, total_oi, computed_at,
                   (expiry - trade_date) AS dte
            FROM {TABLE_OPTION_METRIC_MAX_PAIN_DAILY}
            WHERE symbol = %s
              AND trade_date = (
                  SELECT MAX(trade_date) FROM {TABLE_OPTION_METRIC_MAX_PAIN_DAILY} WHERE symbol = %s
              )
            ORDER BY ABS((expiry - trade_date) - 30) ASC, expiry ASC
            LIMIT 1
            """,
            (symbol, symbol),
        )
        if not row:
            exh.caveats.append("No max-pain rows for symbol")
            return exh
        close_row = _fetch_one(
            conn,
            f"SELECT close::float FROM {STOCK_DAILY} WHERE symbol = %s AND bar_date = %s",
            (symbol, row[0]),
        )
        close = float(close_row[0]) if close_row and close_row[0] is not None else None
        max_pain = float(row[2]) if row[2] is not None else None
        distance = None
        if close and max_pain is not None:
            distance = (close - max_pain) / close
        else:
            exh.caveats.append("No close for the max-pain date — pin distance unknown")
        exh.as_of = iso_date(row[0])
        exh.freshness = freshness_from(row[4], True)
        exh.readings = {
            "pin_pct_distance": distance,
            "max_pain_strike": max_pain,
            "close": close,
            "dte": row[5],
            "expiry": iso_date(row[1]),
            "total_oi": row[3],
        }
        # C2: the magnet is only a claim until its record is next to it — how
        # often this symbol actually settled within 0.5% of max pain.
        try:
            pin_rows = opex_repo.get_pin_analysis(conn, symbol, cycles=PIN_CYCLES)
        except Exception as exc:  # noqa: BLE001 — history must not sink the reading
            logger.debug("pin analysis failed for %s: %s", symbol, exc)
            rollback_quietly(conn)
            pin_rows = []
        exh.history_summary = pin_history(pin_rows)
        if not pin_rows:
            exh.caveats.append("No settled OpEx cycles yet — pin rate unknown")
    except Exception as exc:
        return _failed(exh, "OpEx pin", exc, conn)
    return exh


def exhibit_momentum(conn: Any, symbol: str) -> ExhibitResponse:
    exh = ExhibitResponse(lens="momentum", symbol=symbol)
    try:
        row = _fetch_one(
            conn,
            f"""
            SELECT trade_date, score, grade, path, z_sdt, z_v, accept_vwap, h_52w, computed_at
            FROM {TABLE_STOCK_SIGNAL_MOMENTUM_DAILY}
            WHERE symbol = %s
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (symbol,),
        )
        if not row:
            exh.caveats.append("No momentum rows for symbol")
            return exh
        exh.as_of = iso_date(row[0])
        exh.freshness = freshness_from(row[8], True)
        exh.readings = {
            "score": row[1],
            "grade": row[2],
            "path": row[3],
            "z_sdt": row[4],
            "z_v": row[5],
            "accept_vwap": row[6],
            "h_52w": row[7],
        }
    except Exception as exc:
        return _failed(exh, "Momentum", exc, conn)
    return exh


def exhibit_sepa(conn: Any, symbol: str) -> ExhibitResponse:
    exh = ExhibitResponse(lens="sepa", symbol=symbol)
    try:
        row = _fetch_one(
            conn,
            f"""
            SELECT trade_date, sepa_score, grade, stage, path, fundamental_score,
                   trend_template_score, momentum_score, structure_score, computed_at
            FROM {TABLE_STOCK_SIGNAL_SEPA_DAILY}
            WHERE symbol = %s
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (symbol,),
        )
        if not row:
            exh.caveats.append("No SEPA rows for symbol")
            return exh
        exh.as_of = iso_date(row[0])
        exh.freshness = freshness_from(row[9], True)
        exh.readings = {
            "sepa_score": row[1],
            "grade": row[2],
            "stage": row[3],
            "path": row[4],
            "fundamental_score": row[5],
            "trend_template_score": row[6],
            "momentum_score": row[7],
            "structure_score": row[8],
        }
    except Exception as exc:
        return _failed(exh, "SEPA", exc, conn)
    return exh


def exhibit_forecast_path(conn: Any, symbol: str) -> ExhibitResponse:
    exh = ExhibitResponse(lens="forecast_path", symbol=symbol)
    try:
        row = _fetch_one(
            conn,
            f"""
            SELECT COUNT(*)::bigint,
                   AVG(CASE WHEN path_hit THEN 1.0 ELSE 0.0 END),
                   AVG(ABS(close_miss_pct)),
                   MAX(trade_date),
                   MAX(computed_at)
            FROM {TABLE_STOCK_BACKTEST_SETTLEMENT}
            WHERE symbol = %s AND trade_date >= CURRENT_DATE - INTERVAL '30 days'
            """,
            (symbol,),
        )
        count = int(row[0] or 0) if row else 0
        if not row or count == 0:
            exh.caveats.append("No settled forecast sessions in the last 30 days")
            return exh
        exh.as_of = iso_date(row[3])
        exh.freshness = freshness_from(row[4], True)
        # avg |miss| — the same unsigned mean the Sessions hub and regime-stats
        # report; a signed mean let over- and under-shoots cancel (C3).
        exh.readings = {
            "session_count": count,
            "path_hit_rate": float(row[1]) if row[1] is not None else None,
            "avg_close_miss_pct": float(row[2]) if row[2] is not None else None,
            "window_days": 30,
        }
        exh.history_summary = {"sessions_30d": count}
        exh.caveats.append("Read as a track record; the per-regime split is /research/backtest/regime-stats")
    except Exception as exc:
        return _failed(exh, "Forecast settlement", exc, conn)
    return exh


def exhibit_iv_percentile(conn: Any, symbol: str) -> ExhibitResponse:
    exh = ExhibitResponse(lens="iv_percentile", symbol=symbol)
    try:
        row = _fetch_one(
            conn,
            f"""
            SELECT trade_date, iv_percentile_1y, iv_rank_1y, iv_current, computed_at
            FROM {TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY}
            WHERE symbol = %s
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (symbol,),
        )
        if not row:
            exh.caveats.append("No IV percentile rows for symbol")
            return exh
        exh.as_of = iso_date(row[0])
        exh.freshness = freshness_from(row[4], True)
        exh.readings = {"iv_percentile_1y": row[1], "iv_rank_1y": row[2], "iv_current": row[3]}
    except Exception as exc:
        return _failed(exh, "IV percentile", exc, conn)
    return exh


# ─── enrichment ─────────────────────────────────────────────────────────────


def enrich_exhibit(
    conn: Any,
    exh: ExhibitResponse,
    *,
    lens_id: str,
    value: Any = None,
    fractions_as_pct: bool = False,
) -> ExhibitResponse:
    """Attach ``lens_id``, ``verdict``, ``track_record`` and ``similar`` from the registry."""
    spec = LENSES[lens_id]
    exh.lens_id = lens_id

    tape_missing = (
        spec.data_dependency == TAPE_SOURCE and exh.readings.get("data_source") != TAPE_SOURCE
    )
    if tape_missing:
        if exh.readings:
            exh.caveats.append(
                "No options trades tape on the current data plan — the score is an OI proxy and carries no verdict"
            )
        exh.verdict = None
    else:
        try:
            exh.verdict = verdict_for(lens_id, value, fractions_as_pct=fractions_as_pct)
        except Exception as exc:  # noqa: BLE001 — a verdict must never sink the exhibit
            logger.debug("verdict failed for %s: %s", lens_id, exc)
            exh.verdict = None

    if spec.decay_lens:
        exh.track_record = fetch_track_record(conn, spec.decay_lens, exh.symbol)
        if exh.track_record is None:
            exh.caveats.append("No settled track record for this lens yet")
    else:
        exh.caveats.append("No settled track record for this lens yet")

    if spec.similar_lens and spec.kind != "categorical" and value is not None:
        try:
            rows, source, _ = similar_rows(
                conn,
                lens=spec.similar_lens,
                symbol=exh.symbol,
                value=float(value),
                k=SIMILAR_K,
                horizon=SIMILAR_HORIZON,
            )
            summary = summarize_forward_returns(rows, horizon=SIMILAR_HORIZON)
            summary["lens"] = spec.similar_lens
            summary["source"] = source
            exh.similar = summary
        except Exception as exc:  # noqa: BLE001 — degrade to a caveat
            logger.debug("similar failed for %s: %s", lens_id, exc)
            rollback_quietly(conn)
            exh.caveats.append("Similar-regime summary unavailable")
    return exh


# Which reading a lens is judged on, and whether a [0, 1] value is a fraction of percent.
VERDICT_INPUT: dict[str, tuple[str, bool]] = {
    "iv_rank": ("iv_rank_1y", False),
    "iv_percentile": ("iv_percentile_1y", False),
    "vrp": ("vrp_pct_252d", False),
    "skew": ("slope_pctile_252d", False),
    "term_slope": ("backwardation", False),
    "opex_pin": ("pin_pct_distance", False),
    "gex_regime": ("regime", False),
    "terrain_regime": ("regime", False),
    "momentum": ("score", False),
    "sepa": ("sepa_score", False),
    "order_sentiment": ("sentiment_score", False),
    "forecast_path": ("path_hit_rate", False),
}

NEW_LENS_BUILDERS = {
    "skew": exhibit_skew,
    "term_slope": exhibit_term_slope,
    "gex_regime": exhibit_gex_regime,
    "opex_pin": exhibit_opex_pin,
    "momentum": exhibit_momentum,
    "sepa": exhibit_sepa,
    "forecast_path": exhibit_forecast_path,
    "iv_percentile": exhibit_iv_percentile,
}
