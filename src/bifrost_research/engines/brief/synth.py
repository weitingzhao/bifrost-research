"""Daily Brief synthesis — server-side verdict + card snapshots (Wave R8).

Since research-loop-automation C3 the per-lens cards and the verdict's numbers
come from the exhibit readers (``engines/brief/cards.py``): the same object a
hub view's verdict strip and Copilot read, so the brief cannot disagree with
the page it opens.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence

from bifrost_research.engines.backtest.regime_stats import compute_regime_stats
from bifrost_research.engines.brief.cards import (
    BRIEF_LENSES,
    CARD_LENS,
    exhibit_card,
    forecast_text,
    gex_text,
    has_reading,
    hub_route,
    iv_text,
    lamp_for,
    load_exhibits,
    momentum_text,
    opex_text,
    sepa_text,
    skew_text,
    spot_vs_close,
    term_text,
    terrain_text,
    vrp_text,
)
from bifrost_research.engines.brief.opportunity import (
    TAPE_SOURCE,
    load_sepa_symbol,
    pick_opportunity,
    sentiment_card_verdict,
)
from bifrost_research.lenses.exhibit_model import ExhibitResponse

EVENT_RADAR_ROUTE = "/research/event-radar"

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


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _risk_from_exhibits(
    symbol: str,
    selected_date: str,
    exhibits: Mapping[str, ExhibitResponse],
) -> tuple[str, LampColor, str] | None:
    """The first exhibit that flags a risk, in the order a desk would check them."""
    gex = exhibits.get("gex_regime")
    if has_reading(gex):
        spot, put_wall = _float(gex.readings.get("spot")), _float(gex.readings.get("major_put_wall"))  # type: ignore[union-attr]
        if spot and spot > 0 and put_wall is not None:
            dist_put = ((spot - put_wall) / spot) * 100
            if dist_put < 0.5:
                return (
                    f"Near put wall {put_wall:.0f} ({dist_put:.2f}% from spot)",
                    lamp_for(gex, selected_date),
                    hub_route("gex_regime"),
                )
    skew = exhibits.get("skew")
    if has_reading(skew) and (skew.verdict or {}).get("band") == "hot":  # type: ignore[union-attr]
        pctl = _float(skew.readings.get("slope_pctile_252d"))  # type: ignore[union-attr]
        days = _float(skew.readings.get("history_days"))  # type: ignore[union-attr]
        where = f" ({pctl:.0f}th pctl{f', {days:.0f}d' if days is not None else ''})" if pctl is not None else ""
        return (
            f"Skew at its own-year extreme{where} — wings expensive, size carefully",
            lamp_for(skew, selected_date),
            hub_route("skew"),
        )
    term = exhibits.get("term_slope")
    if has_reading(term) and (term.verdict or {}).get("band") == "hot":  # type: ignore[union-attr]
        back = _float(term.readings.get("backwardation"))  # type: ignore[union-attr]
        pts = f" {back * 100:+.1f} pts" if back is not None else ""
        return (
            f"Backwardation{pts} — event or stress priced up front",
            lamp_for(term, selected_date),
            hub_route("term_slope"),
        )
    iv = exhibits.get("iv_rank")
    if has_reading(iv):
        band = (iv.verdict or {}).get("band")  # type: ignore[union-attr]
        rank = _float(iv.readings.get("iv_rank_1y"))  # type: ignore[union-attr]
        if band in ("hot", "cold") and rank is not None:
            word = "High" if band == "hot" else "Low"
            return (
                f"IV rank {rank:.0f} — {word} vol regime",
                lamp_for(iv, selected_date),
                hub_route("iv_rank"),
            )
    return None


def build_verdict(
    *,
    symbol: str,
    selected_date: str,
    events: list[dict[str, Any]],
    sepa_candidates: list[dict[str, Any]],
    mom_rows: list[dict[str, Any]],
    exhibits: Mapping[str, ExhibitResponse],
    forecast_latest: dict[str, Any] | None = None,
    regime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Narrative / risk / opportunity, each with the hub view that shows the same numbers."""
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

    terrain = exhibits.get("terrain_regime")
    narrative_to = hub_route("terrain_regime")
    if has_reading(terrain):
        r = terrain.readings  # type: ignore[union-attr]
        spot, close = _float(r.get("spot")), _float(r.get("expected_close"))
        gap = f" — {spot_vs_close(spot, close)}" if spot is not None and close is not None else ""
        narrative_text = f"{symbol} {r.get('regime')}{gap}"
        narrative_lamp = lamp_for(terrain, selected_date)
    elif forecast_latest:
        narrative_text = (
            f"{symbol} {forecast_latest.get('regime')} — "
            f"E[close] {float(forecast_latest['expected_close']):.2f}"
        )
        narrative_lamp = freshness_lamp(forecast_latest.get("trade_date"), selected_date, False, True)
        narrative_to = hub_route("forecast_path")
    else:
        narrative_text = f"No terrain narrative for {symbol}"
        narrative_lamp = "gray"

    risk_to = EVENT_RADAR_ROUTE
    high_event = next((e for e in events if (e.get("importance") or 0) >= 3), None)
    if high_event:
        risk_text = (
            high_event.get("subject")
            or high_event.get("event_summary")
            or high_event.get("theme")
            or "High-importance event"
        )
        risk_lamp = events_lamp
    else:
        flagged = _risk_from_exhibits(symbol, selected_date, exhibits)
        if flagged:
            risk_text, risk_lamp, risk_to = flagged
        elif any(has_reading(x) for x in exhibits.values()):
            risk_text = "No elevated event, dealer, skew or vol-regime risk flagged"
            risk_lamp = "green"
        else:
            risk_text = "No risk signals loaded"
            risk_lamp = "gray"

    opportunity_text, opportunity_lamp, opportunity_to, sepa_pick = pick_opportunity(
        symbol=symbol,
        sepa_candidates=sepa_candidates,
        mom_rows=mom_rows,
        sepa_lamp=sepa_lamp,
        mom_lamp=mom_lamp,
    )

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
    forecast_latest: dict[str, Any] | None = None
    own_sepa: dict[str, Any] | None = None
    if resolved:
        sepa_candidates, _ = _load_sepa_candidates(conn, resolved)
        own_sepa = load_sepa_symbol(conn, sym, resolved)
        if own_sepa and not any(str(r.get("symbol") or "").upper() == sym for r in sepa_candidates):
            sepa_candidates = [own_sepa, *sepa_candidates]
        mom_rows, _ = _load_momentum(conn, resolved)
        forecast_latest = _load_forecast_latest(conn, sym, resolved)
    settlement = _load_settlement_latest(conn, sym)

    # C3: every lens through the same reader the hub views and Copilot use.
    exhibits = load_exhibits(conn, sym)
    terrain = exhibits.get("terrain_regime")

    current_regime = (
        terrain.readings.get("regime")  # type: ignore[union-attr]
        if has_reading(terrain)
        else (forecast_latest.get("regime") if forecast_latest else None)
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
        exhibits=exhibits,
        forecast_latest=forecast_latest,
        regime_context=regime_context,
    )

    events_lamp = freshness_lamp(
        events[0].get("collected_at") if events else None,
        selected_date,
        False,
        len(events) > 0,
    )
    freshness: dict[str, LampColor] = {
        "events": events_lamp,
        # The SEPA / Momentum lamps describe the market screeners the brief lists,
        # not the symbol's own row — that one sits on the card.
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
    }
    for key, lens_id in CARD_LENS.items():
        if key not in freshness:
            freshness[key] = lamp_for(exhibits.get(lens_id), selected_date)

    setup_count = sum(1 for r in sepa_candidates if r.get("path") == "SETUP")
    pivot_count = sum(1 for r in sepa_candidates if r.get("path") == "PIVOT")
    grade_counts: dict[str, int] = {"A+": 0, "A": 0, "B": 0}
    for r in mom_rows:
        g = r.get("grade")
        if g in grade_counts:
            grade_counts[g] += 1

    def _card(key: str, text: str, **extra: Any) -> dict[str, Any]:
        lens_id = CARD_LENS[key]
        return exhibit_card(exhibits.get(lens_id), lens_id=lens_id, selected_date=selected_date, text=text, **extra)

    sentiment = exhibits.get("order_sentiment")
    sentiment_row = (
        {**sentiment.readings, "trade_date": sentiment.as_of}  # type: ignore[union-attr]
        if has_reading(sentiment)
        else None
    )

    cards: dict[str, Any] = {
        "terrain": _card("terrain", terrain_text(sym, terrain)),
        "gex": _card("gex", gex_text(sym, exhibits.get("gex_regime"))),
        "opex": _card("opex", opex_text(sym, exhibits.get("opex_pin"))),
        "forecast": _card(
            "forecast",
            forecast_text(sym, exhibits.get("forecast_path"), forecast_latest),
            detail=forecast_latest,
            settlement=settlement,
        ),
        "sepa": _card(
            "sepa",
            sepa_text(sym, exhibits.get("sepa"), setup_count, pivot_count),
            candidates=sepa_candidates[:3],
            own=own_sepa,
        ),
        "momentum": _card(
            "momentum",
            momentum_text(sym, exhibits.get("momentum"), grade_counts),
            sample_symbols=[r.get("symbol") for r in mom_rows[:3]],
            count=len(mom_rows),
        ),
        "iv": _card("iv", iv_text(sym, exhibits.get("iv_rank"))),
        "vrp": _card("vrp", vrp_text(sym, exhibits.get("vrp"))),
        "skew": _card("skew", skew_text(sym, exhibits.get("skew"))),
        "term_slope": _card("term_slope", term_text(sym, exhibits.get("term_slope"))),
        "events": {
            "present": len(events) > 0,
            "verdict": (
                "No event radar rows"
                if not events
                else f"{len(events)} recent · top importance {max(int(e.get('importance') or 0) for e in events)}"
            ),
            "lamp": events_lamp,
            "to": EVENT_RADAR_ROUTE,
            "rows": events[:4],
        },
        "sentiment": _card(
            "sentiment",
            sentiment_card_verdict(sentiment_row, sym),
            tape=bool(sentiment_row and sentiment_row.get("data_source") == TAPE_SOURCE),
            detail=sentiment_row,
        ),
    }

    return {
        "symbol": sym,
        "trade_date": selected_date,
        "verdict": verdict,
        "freshness": freshness,
        "cards": cards,
        "regime_context": regime_context,
        "lenses": list(BRIEF_LENSES),
    }
