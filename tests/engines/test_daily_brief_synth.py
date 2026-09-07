"""Daily Brief — verdict and cards built from the exhibit readers (research-loop-automation C3)."""

from __future__ import annotations

from datetime import date
from typing import Any

from bifrost_research.engines.brief import cards as C
from bifrost_research.engines.brief import synth as S
from bifrost_research.engines.brief.synth import build_verdict, sentiment_card_verdict, synthesize_daily_brief
from bifrost_research.lenses.exhibit_model import ExhibitResponse
from bifrost_research.lenses.registry import LENSES

DAY = "2026-09-04"
OLDER = "2026-09-02"


def _exh(
    lens: str,
    readings: dict[str, Any] | None = None,
    *,
    band: str | None = None,
    means: str | None = None,
    as_of: str = DAY,
    history: dict[str, Any] | None = None,
) -> ExhibitResponse:
    verdict = {"band": band, "label": band.replace("_", " ").title(), "value": None, "unit": "", "means": means} if band else None
    return ExhibitResponse(
        lens=lens,
        lens_id=lens,
        symbol="NVDA",
        as_of=as_of if readings else None,
        freshness="fresh" if readings else "missing",
        readings=readings or {},
        history_summary=history or {},
        verdict=verdict,
    )


def _verdict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        symbol="NVDA",
        selected_date=DAY,
        events=[],
        sepa_candidates=[],
        mom_rows=[],
        exhibits={},
        forecast_latest=None,
        regime_context=None,
    )
    base.update(over)
    return build_verdict(**base)


TERRAIN = _exh("terrain_regime", {"regime": "range", "pin_score": 40, "tail_risk": 7, "spot": 230.36, "expected_close": 224.27}, band="neutral")


# ─── verdict ─────────────────────────────────────────────────────────────────


def test_narrative_reads_the_terrain_exhibit_and_links_to_its_hub_view() -> None:
    v = _verdict(exhibits={"terrain_regime": TERRAIN})
    assert v["narrative"]["text"] == "NVDA range — spot 230.36 above E[close] 224.27 (+2.72%)"
    assert v["narrative"]["lamp"] == "green"
    assert v["narrative"]["to"] == LENSES["terrain_regime"].page_route
    older = _verdict(exhibits={"terrain_regime": _exh("terrain_regime", dict(TERRAIN.readings), as_of=OLDER)})
    assert older["narrative"]["lamp"] == "yellow"


def test_high_importance_event_outranks_every_exhibit_risk() -> None:
    events = [{"importance": 5, "subject": "Fed decision", "collected_at": DAY}]
    gex = _exh("gex_regime", {"spot": 100.2, "major_put_wall": 100.0, "zero_gamma": 101, "major_call_wall": 105})
    v = _verdict(events=events, exhibits={"gex_regime": gex})
    assert v["risk"]["text"] == "Fed decision"
    assert v["risk"]["to"] == "/research/event-radar"
    assert v["action_hint"]["label"] == "Review risk" or v["risk"]["lamp"] == "green"


def test_risk_walks_put_wall_then_skew_then_term_then_iv() -> None:
    gex_near = _exh("gex_regime", {"spot": 100.2, "major_put_wall": 100.0}, band="cold")
    skew_hot = _exh("skew", {"atm_slope": -0.2, "slope_pctile_252d": 94.0, "history_days": 120}, band="hot")
    term_hot = _exh("term_slope", {"backwardation": 0.041}, band="hot")
    iv_cold = _exh("iv_rank", {"iv_rank_1y": 18.7}, band="cold")

    near = _verdict(exhibits={"gex_regime": gex_near, "skew": skew_hot, "term_slope": term_hot, "iv_rank": iv_cold})
    assert near["risk"]["text"].startswith("Near put wall 100 (0.20% from spot)")
    assert near["risk"]["to"] == LENSES["gex_regime"].page_route

    skew = _verdict(exhibits={"skew": skew_hot, "term_slope": term_hot, "iv_rank": iv_cold})
    assert skew["risk"]["text"] == "Skew at its own-year extreme (94th pctl, 120d) — wings expensive, size carefully"
    assert skew["risk"]["to"] == LENSES["skew"].page_route

    term = _verdict(exhibits={"term_slope": term_hot, "iv_rank": iv_cold})
    assert term["risk"]["text"] == "Backwardation +4.1 pts — event or stress priced up front"

    iv = _verdict(exhibits={"iv_rank": iv_cold})
    assert iv["risk"]["text"] == "IV rank 19 — Low vol regime"
    assert iv["risk"]["to"] == LENSES["iv_rank"].page_route

    calm = _verdict(exhibits={"iv_rank": _exh("iv_rank", {"iv_rank_1y": 45}, band="neutral")})
    assert calm["risk"] == {"label": "Key risk", "text": "No elevated event, dealer, skew or vol-regime risk flagged", "lamp": "green", "to": "/research/event-radar"}

    nothing = _verdict()
    assert nothing["risk"]["lamp"] == "gray" and nothing["narrative"]["lamp"] == "gray"


def test_build_verdict_regime_meta() -> None:
    v = _verdict(
        exhibits={"terrain_regime": TERRAIN},
        regime_context={"lookback_days": 60, "current_regime": {"regime": "range", "path_hit_rate": 0.0, "sample_n": 15}},
    )
    assert v["narrative"]["meta"] == "Same regime (range): 0% path hit · n=15 · 60d"


# ─── opportunity (A4 rules, unchanged) ───────────────────────────────────────


def _sepa(symbol: str, path: str, grade: str = "A", score: float = 80.0) -> dict[str, Any]:
    return {"symbol": symbol, "path": path, "grade": grade, "sepa_score": score, "trade_date": DAY}


def test_opportunity_is_the_symbols_own_setup_when_it_has_one() -> None:
    v = _verdict(sepa_candidates=[_sepa("HIMS", "SETUP", "A+", 92), _sepa("NVDA", "PIVOT", "A", 82)])
    assert v["opportunity"]["text"] == "SEPA NVDA PIVOT · grade A · score 82"
    assert v["action_hint"]["label"] == "View opportunity"


def test_a_single_name_without_a_setup_says_so_instead_of_borrowing_another_ticker() -> None:
    v = _verdict(sepa_candidates=[_sepa("HIMS", "SETUP", "A+", 92)])
    assert v["opportunity"]["text"].startswith("No SEPA setup for NVDA · top today: SEPA HIMS SETUP")
    assert v["opportunity"]["lamp"] == "gray"
    assert v["action_hint"]["label"] == "Open narrative"


def test_market_wide_symbol_still_reads_the_markets_best_setup() -> None:
    v = _verdict(symbol="SPY", sepa_candidates=[_sepa("HIMS", "SETUP", "A+", 92)])
    assert v["opportunity"]["text"] == "SEPA HIMS SETUP · grade A+ · score 92"


def test_sentiment_card_is_honest_about_the_tape() -> None:
    assert sentiment_card_verdict(None, "NVDA") == "No sentiment for NVDA"
    proxy = {"trade_date": DAY, "data_source": "oi_proxy", "sentiment_score": 12.0}
    assert sentiment_card_verdict(proxy, "NVDA") == f"No tape — OI proxy only, no verdict · date {DAY}"
    tape = {"trade_date": DAY, "data_source": "option_trades_tape", "sentiment_score": -8.0}
    assert sentiment_card_verdict(tape, "NVDA") == f"Net bias -8 (tape) · date {DAY}"


# ─── cards ───────────────────────────────────────────────────────────────────


def test_spot_vs_close_word_and_sign_describe_the_same_gap() -> None:
    assert C.spot_vs_close(230.36, 224.27) == "spot 230.36 above E[close] 224.27 (+2.72%)"
    assert C.spot_vs_close(220.0, 224.27) == "spot 220.00 below E[close] 224.27 (-1.90%)"


def test_lamp_follows_the_exhibit_as_of_against_the_selected_date() -> None:
    assert C.lamp_for(None, DAY) == "gray"
    assert C.lamp_for(_exh("vrp"), DAY) == "gray"
    assert C.lamp_for(_exh("vrp", {"vrp_pct_252d": 21}), DAY) == "green"
    assert C.lamp_for(_exh("vrp", {"vrp_pct_252d": 21}, as_of=OLDER), DAY) == "yellow"


def test_card_carries_the_exhibit_verdict_readings_and_hub_route() -> None:
    gex = _exh(
        "gex_regime",
        {
            "regime": "positive",
            "spot": 230.36,
            "spot_vs_zero_gamma": "below",
            "zero_gamma": 246.2,
            "major_put_wall": 200.0,
            "major_call_wall": 250.0,
            "vrp_link": {"rv_20d": 0.448, "atm_iv_30d": 0.3367, "consistent": False},
        },
        band="cold",
        means="Positive net gamma — dealers damp moves; realised vol compresses.",
    )
    card = C.exhibit_card(gex, lens_id="gex_regime", selected_date=DAY, text=C.gex_text("NVDA", gex))
    assert card["verdict"] == (
        "Spot 230 below zero-γ 246 · put 200 / call 250 · RV20 44.8% vs IV30 33.7% — realised vol contradicts the regime"
    )
    assert card["band"] == "cold" and card["label"] == "Cold" and card["means"].startswith("Positive net gamma")
    assert card["to"] == "/research/dealer-levels?view=gex" == LENSES["gex_regime"].page_route
    assert card["lamp"] == "green" and card["as_of"] == DAY and card["present"] is True
    assert card["readings"]["zero_gamma"] == 246.2
    assert card["detail"]["trade_date"] == DAY and card["detail"]["spot"] == 230.36

    empty = C.exhibit_card(None, lens_id="gex_regime", selected_date=DAY, text=C.gex_text("NVDA", None))
    assert empty["present"] is False and empty["verdict"] == "No GEX levels for NVDA" and empty["detail"] is None


def test_card_lines_read_the_c2_depth() -> None:
    opex = _exh(
        "opex_pin",
        {"max_pain_strike": 220.0, "close": 230.36, "pin_pct_distance": 0.045},
        history={"cycles": 1, "pinned": 0, "pin_rate": 0.0},
    )
    assert C.opex_text("NVDA", opex) == "Max pain 220 · 4.5% below spot · pinned 0 of 1 cycles"
    skew = _exh("skew", {"atm_slope": -0.00922, "slope_pctile_252d": 0.0, "history_days": 9, "atm_vol": 0.4176})
    assert C.skew_text("NVDA", skew) == "ATM slope -0.009 (call skew) · bottom of own year (9d) · ATM vol 41.8%"
    term = _exh("term_slope", {"term_structure": "backwardation", "backwardation": 0.04126, "near_vol": 0.4176, "near_dte": 28, "far_vol": 0.3763, "far_dte": 77})
    assert C.term_text("NVDA", term) == "Backwardation +4.1 pts · near 41.8% (28d) vs far 37.6% (77d)"
    vrp = _exh("vrp", {"vrp_pct_252d": 20.73, "atm_iv_30d": 0.3367, "rv_60d": 0.4056, "vrp_60d": -0.0689})
    assert C.vrp_text("NVDA", vrp) == "VRP 21st pctl · IV30 33.7% vs RV60 40.6% · spread -6.9%"
    fc = _exh("forecast_path", {"session_count": 15, "path_hit_rate": 0.0, "avg_close_miss_pct": 0.0204})
    assert C.forecast_text("NVDA", fc, {"regime": "range", "expected_close": 225.4}) == (
        "range · E[close] 225.40 · 30d path hit 0% across 15 settled · avg |miss| 2.0%"
    )
    assert C.forecast_text("NVDA", None, None) == "No settled forecast sessions for NVDA in 30d"
    assert C.iv_text("NVDA", _exh("iv_rank", {"iv_rank_1y": 18.7, "iv_percentile_1y": 16.7, "iv_current": 0.384})) == "IV Rank 19 · pctl 17 · IV 38.4%"
    assert C.terrain_text("NVDA", TERRAIN) == "range · pin 40 · tail 7 · spot 230.36 above E[close] 224.27 (+2.72%)"
    assert C.sepa_text("NVDA", _exh("sepa", {"path": "PIVOT", "grade": "A", "sepa_score": 82}), 6, 14) == (
        "NVDA PIVOT · grade A · score 82 · market Setup 6 · Pivot 14"
    )
    assert C.momentum_text("NVDA", None, {"A+": 0, "A": 0, "B": 6}) == "No momentum row for NVDA · market A+ 0 · A 0 · B 6"


def test_a_failing_reader_becomes_a_missing_exhibit_not_a_missing_brief() -> None:
    def builder(conn: Any, lens: str, symbol: str) -> ExhibitResponse:
        if lens == "skew":
            raise RuntimeError("surface table gone")
        return _exh(lens, {"x": 1})

    class _Conn:
        def rollback(self) -> None:
            pass

    out = C.load_exhibits(_Conn(), "NVDA", lenses=("vrp", "skew"), builder=builder)
    assert out["vrp"].freshness == "fresh"
    assert out["skew"].freshness == "missing" and "surface table gone" in out["skew"].caveats[0]


# ─── one source, end to end ──────────────────────────────────────────────────


def test_synthesize_daily_brief_builds_every_card_from_the_exhibits(monkeypatch) -> None:
    day = date(2026, 9, 4)
    gex = _exh("gex_regime", {"regime": "positive", "spot": 230.36, "spot_vs_zero_gamma": "below", "zero_gamma": 246.2, "major_put_wall": 200.0, "major_call_wall": 250.0}, band="cold")
    exhibits = {
        "terrain_regime": TERRAIN,
        "gex_regime": gex,
        "vrp": _exh("vrp", {"vrp_pct_252d": 20.7, "atm_iv_30d": 0.3367, "rv_60d": 0.4056, "vrp_60d": -0.069}, band="lean_cold", as_of=OLDER),
        "order_sentiment": _exh("order_sentiment", {"data_source": "oi_proxy", "sentiment_score": 3.0}),
    }
    monkeypatch.setattr(S, "_resolve_trade_date", lambda conn, sym, td: day)
    monkeypatch.setattr(S, "_load_events", lambda conn, limit=8: [])
    monkeypatch.setattr(S, "_load_sepa_candidates", lambda conn, td, top=20: ([], td))
    monkeypatch.setattr(S, "load_sepa_symbol", lambda conn, sym, td: None)
    monkeypatch.setattr(S, "_load_momentum", lambda conn, td, limit=200: ([], td))
    monkeypatch.setattr(S, "_load_forecast_latest", lambda conn, sym, td: None)
    monkeypatch.setattr(S, "_load_settlement_latest", lambda conn, sym: None)
    monkeypatch.setattr(S, "compute_regime_stats", lambda conn, sym, **kw: None)
    monkeypatch.setattr(S, "load_exhibits", lambda conn, sym: exhibits)

    brief = synthesize_daily_brief(object(), "nvda")

    assert brief["symbol"] == "NVDA" and brief["trade_date"] == DAY
    assert brief["lenses"] == list(C.BRIEF_LENSES)
    cards = brief["cards"]
    # the card and the verdict quote the same exhibit
    assert cards["gex"]["verdict"] == C.gex_text("NVDA", gex)
    assert cards["gex"]["band"] == "cold" and cards["gex"]["to"] == LENSES["gex_regime"].page_route
    assert brief["freshness"]["gex"] == cards["gex"]["lamp"] == "green"
    assert brief["freshness"]["vrp"] == cards["vrp"]["lamp"] == "yellow"
    assert brief["freshness"]["skew"] == "gray" and cards["skew"]["present"] is False
    assert brief["verdict"]["narrative"]["text"].startswith("NVDA range — spot 230.36")
    assert cards["terrain"]["detail"]["trade_date"] == DAY
    assert cards["sentiment"]["verdict"] == f"No tape — OI proxy only, no verdict · date {DAY}"
    assert cards["sentiment"]["tape"] is False
    assert cards["events"] == {"present": False, "verdict": "No event radar rows", "lamp": "gray", "to": "/research/event-radar", "rows": []}
    assert set(C.CARD_LENS) <= set(cards)
