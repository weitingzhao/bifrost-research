"""Daily Brief synthesis — server-side verdict + card snapshots (Wave R8)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence

from bifrost_research.engines.backtest.regime_stats import compute_regime_stats

LampColor = str  # green | yellow | red | gray


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    if not s:
        return None
    return date.fromisoformat(s)


def _date_prefix(value: Any) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    return s[:10] if len(s) >= 10 else None


def freshness_lamp(
    trade_date: Any,
    selected_date: str,
    has_error: bool,
    has_data: bool,
) -> LampColor:
    if has_error:
        return "red"
    if not has_data:
        return "gray"
    td = _date_prefix(trade_date)
    target = selected_date or date.today().isoformat()
    if not td:
        return "yellow"
    if td == target:
        return "green"
    return "yellow"


def iv_bucket(rank: float | None) -> str:
    if rank is None or not isinstance(rank, (int, float)):
        return "no row"
    if rank > 60:
        return "High"
    if rank >= 30:
        return "Neutral"
    return "Low"


def _spot_vs_close(spot: float, close: float) -> str:
    pct = ((close - spot) / max(spot, 1.0)) * 100
    dir_word = "above" if pct >= 0 else "below"
    sign = "+" if pct >= 0 else ""
    return f"spot {spot:.2f} {dir_word} E[close] {close:.2f} ({sign}{pct:.2f}%)"


def _row_to_dict(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def _resolve_trade_date(conn: Any, symbol: str, trade_date: date | None) -> date | None:
    if trade_date is not None:
        return trade_date
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(trade_date) FROM (
                SELECT trade_date FROM features.stock_forecast_terrain_daily WHERE symbol = %s
                UNION ALL
                SELECT trade_date FROM features.stock_forecast_session WHERE symbol = %s
            ) t
            """,
            (sym, sym),
        )
        row = cur.fetchone()
    if row is None:
        return None
    val = row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
    return _as_date(val)


def _load_events(conn: Any, limit: int = 8) -> list[dict[str, Any]]:
    cols = (
        "event_id",
        "batch_id",
        "collected_at",
        "source",
        "subject",
        "event_summary",
        "theme",
        "importance",
        "event_date",
        "computed_at",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.event_signal_radar_daily
            WHERE dropped IS NULL OR dropped = false
            ORDER BY collected_at DESC NULLS LAST, importance DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        raw = cur.fetchall() or []
    return [_row_to_dict(r, cols) for r in raw]


def _load_sepa_candidates(conn: Any, trade_date: date, top: int = 20) -> tuple[list[dict[str, Any]], date | None]:
    cols = (
        "symbol",
        "trade_date",
        "fundamental_score",
        "trend_template_score",
        "momentum_score",
        "structure_score",
        "sepa_score",
        "grade",
        "stage",
        "path",
        "trend_template_pass",
        "fundamental_pass",
        "latest_close",
        "sma_50",
        "sma_150",
        "sma_200",
        "high_52w",
        "low_52w",
        "iv_percentile",
        "pcr_oi",
        "fund_pass_count",
        "tech_pass_count",
        "factors_json",
        "asof_ts",
        "computed_at",
    )
    resolved = trade_date
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.stock_signal_sepa_daily
            WHERE trade_date = %s AND path IN ('SETUP', 'PIVOT')
            ORDER BY sepa_score DESC NULLS LAST
            LIMIT %s
            """,
            (resolved, top),
        )
        raw = cur.fetchall() or []
    rows = [_row_to_dict(r, cols) for r in raw]
    for r in rows:
        if isinstance(r.get("trade_date"), date):
            r["trade_date"] = r["trade_date"].isoformat()
    return rows, resolved


def _load_momentum(conn: Any, trade_date: date, limit: int = 200) -> tuple[list[dict[str, Any]], date | None]:
    cols = (
        "symbol",
        "trade_date",
        "score",
        "grade",
        "path",
        "z_sdt",
        "z_v",
        "accept_vwap",
        "z_ofi",
        "h_52w",
        "o_plus",
        "a_factor",
        "r_sec",
        "crash",
        "factors_json",
        "computed_at",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.stock_signal_momentum_daily
            WHERE trade_date = %s
            ORDER BY score DESC NULLS LAST, symbol ASC
            LIMIT %s
            """,
            (trade_date, limit),
        )
        raw = cur.fetchall() or []
    rows = [_row_to_dict(r, cols) for r in raw]
    for r in rows:
        if isinstance(r.get("trade_date"), date):
            r["trade_date"] = r["trade_date"].isoformat()
    return rows, trade_date


def _load_iv(conn: Any, symbol: str) -> dict[str, Any] | None:
    cols = (
        "symbol",
        "trade_date",
        "iv_current",
        "iv_percentile_1y",
        "iv_rank_1y",
        "computed_at",
    )
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.option_metric_iv_percentile_daily
            WHERE symbol = %s
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (sym,),
        )
        raw = cur.fetchone()
    if raw is None:
        return None
    row = _row_to_dict(raw, cols)
    if isinstance(row.get("trade_date"), date):
        row["trade_date"] = row["trade_date"].isoformat()
    return row


def _load_terrain(conn: Any, symbol: str, trade_date: date) -> dict[str, Any] | None:
    cols = (
        "symbol",
        "trade_date",
        "pin_score",
        "trend_release",
        "vol_squeeze",
        "tail_risk",
        "expected_close",
        "gamma_zone_low",
        "gamma_zone_high",
        "regime",
        "spot",
        "inputs_json",
        "computed_at",
    )
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.stock_forecast_terrain_daily
            WHERE symbol = %s AND trade_date = %s
            LIMIT 1
            """,
            (sym, trade_date),
        )
        raw = cur.fetchone()
    if raw is None:
        return None
    row = _row_to_dict(raw, cols)
    if isinstance(row.get("trade_date"), date):
        row["trade_date"] = row["trade_date"].isoformat()
    return row


def _load_gex_latest(conn: Any, symbol: str, trade_date: date) -> dict[str, Any] | None:
    cols = (
        "symbol",
        "trade_date",
        "asof_ts",
        "spot",
        "total_net_gex",
        "zero_gamma",
        "major_call_wall",
        "major_put_wall",
        "levels_json",
        "computed_at",
    )
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.option_metric_gex_intraday
            WHERE symbol = %s AND trade_date = %s
            ORDER BY asof_ts ASC
            """,
            (sym, trade_date),
        )
        raw = cur.fetchall() or []
    if not raw:
        return None
    row = _row_to_dict(raw[-1], cols)
    if isinstance(row.get("trade_date"), date):
        row["trade_date"] = row["trade_date"].isoformat()
    if isinstance(row.get("asof_ts"), datetime):
        row["asof_ts"] = row["asof_ts"].isoformat()
    return row


def _load_forecast_latest(conn: Any, symbol: str, trade_date: date) -> dict[str, Any] | None:
    cols = (
        "session_id",
        "symbol",
        "trade_date",
        "regime",
        "spot",
        "prob_rangy",
        "prob_bull",
        "prob_bear",
        "prob_squeeze",
        "expected_close",
        "structures_json",
        "narrative",
        "llm_provider",
        "advisory",
        "computed_at",
    )
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.stock_forecast_session
            WHERE symbol = %s AND trade_date = %s
            ORDER BY computed_at DESC
            LIMIT 1
            """,
            (sym, trade_date),
        )
        raw = cur.fetchone()
    if raw is None:
        return None
    row = _row_to_dict(raw, cols)
    if isinstance(row.get("trade_date"), date):
        row["trade_date"] = row["trade_date"].isoformat()
    return row


def _load_settlement_latest(conn: Any, symbol: str) -> dict[str, Any] | None:
    cols = (
        "settlement_id",
        "session_id",
        "symbol",
        "trade_date",
        "expected_close",
        "actual_close",
        "close_miss",
        "close_miss_pct",
        "path_hit",
        "path_hit_count",
        "path_total",
        "stats_json",
        "notes",
        "computed_at",
    )
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.stock_backtest_settlement
            WHERE symbol = %s
            ORDER BY trade_date DESC, computed_at DESC
            LIMIT 1
            """,
            (sym,),
        )
        raw = cur.fetchone()
    if raw is None:
        return None
    row = _row_to_dict(raw, cols)
    if isinstance(row.get("trade_date"), date):
        row["trade_date"] = row["trade_date"].isoformat()
    return row


def _load_sentiment(conn: Any, symbol: str, trade_date: date) -> dict[str, Any] | None:
    cols = (
        "symbol",
        "trade_date",
        "sentiment_score",
        "computed_at",
    )
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.option_flow_sentiment_daily
            WHERE symbol = %s AND trade_date = %s
            ORDER BY computed_at DESC
            LIMIT 1
            """,
            (sym, trade_date),
        )
        raw = cur.fetchone()
    if raw is None:
        return None
    row = _row_to_dict(raw, cols)
    if isinstance(row.get("trade_date"), date):
        row["trade_date"] = row["trade_date"].isoformat()
    return row


def build_verdict(
    *,
    symbol: str,
    selected_date: str,
    events: list[dict[str, Any]],
    sepa_candidates: list[dict[str, Any]],
    mom_rows: list[dict[str, Any]],
    iv_row: dict[str, Any] | None,
    terrain: dict[str, Any] | None,
    gex_latest: dict[str, Any] | None,
    forecast_latest: dict[str, Any] | None,
    regime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    events_lamp = freshness_lamp(
        events[0].get("collected_at") or events[0].get("computed_at") if events else None,
        selected_date,
        False,
        len(events) > 0,
    )
    sepa_lamp = freshness_lamp(
        sepa_candidates[0].get("trade_date") if sepa_candidates else None,
        selected_date,
        False,
        len(sepa_candidates) > 0,
    )
    mom_lamp = freshness_lamp(
        mom_rows[0].get("trade_date") if mom_rows else None,
        selected_date,
        False,
        len(mom_rows) > 0,
    )
    iv_lamp = freshness_lamp(iv_row.get("trade_date") if iv_row else None, selected_date, False, iv_row is not None)
    terrain_lamp = freshness_lamp(
        terrain.get("trade_date") if terrain else None,
        selected_date,
        False,
        terrain is not None,
    )
    gex_lamp = freshness_lamp(
        gex_latest.get("trade_date") or gex_latest.get("asof_ts") if gex_latest else None,
        selected_date,
        False,
        gex_latest is not None,
    )
    forecast_lamp = freshness_lamp(
        forecast_latest.get("trade_date") if forecast_latest else None,
        selected_date,
        False,
        forecast_latest is not None,
    )

    narrative_to = "/research/analysis-model"
    if terrain:
        narrative_text = f"{symbol} {terrain.get('regime')} — {_spot_vs_close(float(terrain['spot']), float(terrain['expected_close']))}"
        narrative_lamp = terrain_lamp
    elif forecast_latest:
        narrative_text = (
            f"{symbol} {forecast_latest.get('regime')} — "
            f"E[close] {float(forecast_latest['expected_close']):.2f}"
        )
        narrative_lamp = forecast_lamp
        narrative_to = "/research/forecast-sessions"
    else:
        narrative_text = f"No terrain narrative for {symbol}"
        narrative_lamp = "gray"

    risk_to = "/research/event-radar"
    high_event = next((e for e in events if (e.get("importance") or 0) >= 3), None)
    if high_event:
        risk_text = (
            high_event.get("subject")
            or high_event.get("event_summary")
            or high_event.get("theme")
            or "High-importance event"
        )
        risk_lamp = events_lamp
    elif gex_latest and float(gex_latest.get("spot") or 0) > 0:
        spot = float(gex_latest["spot"])
        dist_put = ((spot - float(gex_latest["major_put_wall"])) / spot) * 100
        if dist_put < 0.5:
            risk_text = f"Near put wall {float(gex_latest['major_put_wall']):.0f} ({dist_put:.2f}% from spot)"
            risk_lamp = gex_lamp
            risk_to = "/research/gex-intraday"
        elif iv_row and iv_row.get("iv_rank_1y") is not None:
            rank = float(iv_row["iv_rank_1y"])
            bucket = iv_bucket(rank)
            if bucket in ("High", "Low"):
                risk_text = f"IV rank {rank:.0f} — {bucket} vol regime"
                risk_lamp = iv_lamp
                risk_to = "/research/iv-radar"
            else:
                risk_text = "No elevated event or GEX tail risk flagged"
                risk_lamp = "green"
        else:
            risk_text = "No elevated event or GEX tail risk flagged"
            risk_lamp = "green"
    elif iv_row and iv_row.get("iv_rank_1y") is not None:
        rank = float(iv_row["iv_rank_1y"])
        bucket = iv_bucket(rank)
        risk_text = f"IV rank {rank:.0f} — {bucket}"
        risk_lamp = iv_lamp
        risk_to = "/research/iv-radar"
    else:
        risk_text = "No risk signals loaded"
        risk_lamp = "gray"

    opportunity_to = "/research/sepa-daily-core"
    setup_first = next((r for r in sepa_candidates if r.get("path") == "SETUP"), None)
    pivot_first = next((r for r in sepa_candidates if r.get("path") == "PIVOT"), None)
    sepa_pick = setup_first or pivot_first or (sepa_candidates[0] if sepa_candidates else None)

    if sepa_pick:
        opportunity_text = f"SEPA {sepa_pick.get('symbol')} {sepa_pick.get('path')} · grade {sepa_pick.get('grade')}"
        opportunity_lamp = sepa_lamp
    else:
        a_plus = next((r for r in mom_rows if r.get("grade") == "A+"), None)
        if a_plus:
            opportunity_text = f"Momentum {a_plus.get('symbol')} A+ · score {float(a_plus.get('score') or 0):.0f}"
            opportunity_lamp = mom_lamp
            opportunity_to = "/research/momentum-radar"
        else:
            opportunity_text = "No SEPA / Momentum opportunity today"
            opportunity_lamp = "gray"

    action_hint = {"label": "Open narrative", "to": narrative_to}
    if risk_lamp in ("red", "yellow"):
        action_hint = {"label": "Review risk", "to": risk_to}
    elif opportunity_lamp == "green" and sepa_pick:
        action_hint = {"label": "View opportunity", "to": opportunity_to}

    regime_meta: str | None = None
    if regime_context and regime_context.get("current_regime"):
        cur = regime_context["current_regime"]
        n = int(cur.get("sample_n") or 0)
        rate = float(cur.get("path_hit_rate") or 0)
        days = int(regime_context.get("lookback_days") or 60)
        if n < 5:
            regime_meta = f"Same regime ({cur.get('regime')}): low sample (n={n}) · {days}d"
        else:
            regime_meta = f"Same regime ({cur.get('regime')}): {rate * 100:.0f}% path hit · n={n} · {days}d"

    return {
        "narrative": {
            "label": "Main narrative",
            "text": narrative_text,
            "lamp": narrative_lamp,
            "to": narrative_to,
            "meta": regime_meta,
        },
        "risk": {
            "label": "Key risk",
            "text": risk_text,
            "lamp": risk_lamp,
            "to": risk_to,
        },
        "opportunity": {
            "label": "Opportunity",
            "text": opportunity_text,
            "lamp": opportunity_lamp,
            "to": opportunity_to,
        },
        "action_hint": action_hint,
    }


def synthesize_daily_brief(conn: Any, symbol: str, trade_date: date | None = None) -> dict[str, Any]:
    sym = symbol.strip().upper()
    resolved = _resolve_trade_date(conn, sym, trade_date)
    selected_date = resolved.isoformat() if resolved else date.today().isoformat()

    events = _load_events(conn)
    sepa_candidates: list[dict[str, Any]] = []
    mom_rows: list[dict[str, Any]] = []
    iv_row: dict[str, Any] | None = None
    terrain: dict[str, Any] | None = None
    gex_latest: dict[str, Any] | None = None
    forecast_latest: dict[str, Any] | None = None
    settlement: dict[str, Any] | None = None
    sentiment: dict[str, Any] | None = None

    if resolved:
        sepa_candidates, _ = _load_sepa_candidates(conn, resolved)
        mom_rows, _ = _load_momentum(conn, resolved)
        iv_row = _load_iv(conn, sym)
        terrain = _load_terrain(conn, sym, resolved)
        gex_latest = _load_gex_latest(conn, sym, resolved)
        forecast_latest = _load_forecast_latest(conn, sym, resolved)
        sentiment = _load_sentiment(conn, sym, resolved)

    settlement = _load_settlement_latest(conn, sym)

    current_regime = terrain.get("regime") if terrain else (
        forecast_latest.get("regime") if forecast_latest else None
    )
    regime_context = compute_regime_stats(
        conn,
        sym,
        lookback_days=60,
        current_regime=str(current_regime) if current_regime else None,
    )

    verdict = build_verdict(
        symbol=sym,
        selected_date=selected_date,
        events=events,
        sepa_candidates=sepa_candidates,
        mom_rows=mom_rows,
        iv_row=iv_row,
        terrain=terrain,
        gex_latest=gex_latest,
        forecast_latest=forecast_latest,
        regime_context=regime_context,
    )

    freshness = {
        "events": freshness_lamp(
            events[0].get("collected_at") if events else None,
            selected_date,
            False,
            len(events) > 0,
        ),
        "sepa": freshness_lamp(
            sepa_candidates[0].get("trade_date") if sepa_candidates else None,
            selected_date,
            False,
            len(sepa_candidates) > 0,
        ),
        "momentum": freshness_lamp(
            mom_rows[0].get("trade_date") if mom_rows else None,
            selected_date,
            False,
            len(mom_rows) > 0,
        ),
        "iv": freshness_lamp(iv_row.get("trade_date") if iv_row else None, selected_date, False, iv_row is not None),
        "terrain": freshness_lamp(
            terrain.get("trade_date") if terrain else None,
            selected_date,
            False,
            terrain is not None,
        ),
        "gex": freshness_lamp(
            gex_latest.get("trade_date") if gex_latest else None,
            selected_date,
            False,
            gex_latest is not None,
        ),
        "forecast": freshness_lamp(
            forecast_latest.get("trade_date") if forecast_latest else None,
            selected_date,
            False,
            forecast_latest is not None,
        ),
        "sentiment": freshness_lamp(
            sentiment.get("trade_date") if sentiment else None,
            selected_date,
            False,
            sentiment is not None,
        ),
    }

    setup_count = sum(1 for r in sepa_candidates if r.get("path") == "SETUP")
    pivot_count = sum(1 for r in sepa_candidates if r.get("path") == "PIVOT")
    grade_counts: dict[str, int] = {"A+": 0, "A": 0, "B": 0}
    for r in mom_rows:
        g = r.get("grade")
        if g in grade_counts:
            grade_counts[g] += 1

    cards = {
        "terrain": {
            "present": terrain is not None,
            "verdict": (
                f"No terrain for {sym}"
                if terrain is None
                else (
                    f"{terrain.get('regime')} · pin {float(terrain.get('pin_score') or 0):.0f} · "
                    f"tail {float(terrain.get('tail_risk') or 0):.0f}"
                )
            ),
            "detail": terrain,
        },
        "gex": {
            "present": gex_latest is not None,
            "verdict": (
                f"No GEX snapshots for {sym}"
                if gex_latest is None
                else (
                    f"Spot {float(gex_latest['spot']):.0f} vs call "
                    f"{float(gex_latest['major_call_wall']):.0f} / 0γ "
                    f"{float(gex_latest['zero_gamma']):.0f} / put "
                    f"{float(gex_latest['major_put_wall']):.0f}"
                )
            ),
            "detail": gex_latest,
        },
        "forecast": {
            "present": forecast_latest is not None,
            "verdict": (
                f"No forecast session for {sym}"
                if forecast_latest is None
                else (
                    f"{forecast_latest.get('regime')} · "
                    f"E[close] {float(forecast_latest['expected_close']):.2f}"
                )
            ),
            "detail": forecast_latest,
            "settlement": settlement,
        },
        "sepa": {
            "present": len(sepa_candidates) > 0,
            "verdict": (
                "No Setup/Pivot candidates"
                if not sepa_candidates
                else f"Setup {setup_count} · Pivot {pivot_count}"
            ),
            "candidates": sepa_candidates[:3],
        },
        "momentum": {
            "present": len(mom_rows) > 0,
            "verdict": (
                "No momentum rows"
                if not mom_rows
                else f"A+ {grade_counts['A+']} · A {grade_counts['A']} · B {grade_counts['B']}"
            ),
            "sample_symbols": [r.get("symbol") for r in mom_rows[:3]],
            "count": len(mom_rows),
        },
        "iv": {
            "present": iv_row is not None,
            "verdict": (
                f"No IV row for {sym}"
                if iv_row is None
                else f"Rank {float(iv_row.get('iv_rank_1y') or 0):.0f} · {iv_bucket(float(iv_row.get('iv_rank_1y') or 0))}"
            ),
            "detail": iv_row,
        },
        "events": {
            "present": len(events) > 0,
            "verdict": (
                "No event radar rows"
                if not events
                else f"{len(events)} recent · top importance {max(int(e.get('importance') or 0) for e in events)}"
            ),
            "rows": events[:4],
        },
        "sentiment": {
            "present": sentiment is not None,
            "verdict": (
                f"No sentiment for {sym}"
                if sentiment is None
                else f"Net bias proxy · date {sentiment.get('trade_date') or '—'}"
            ),
            "detail": sentiment,
        },
    }

    return {
        "symbol": sym,
        "trade_date": selected_date,
        "verdict": verdict,
        "freshness": freshness,
        "cards": cards,
        "regime_context": regime_context,
    }
