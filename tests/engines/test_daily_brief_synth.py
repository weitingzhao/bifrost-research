"""Daily Brief synth verdict rules."""

from __future__ import annotations

from bifrost_research.engines.brief.synth import build_verdict


def test_build_verdict_high_importance_event() -> None:
    events = [{"importance": 5, "subject": "Fed decision", "collected_at": "2026-08-25"}]
    verdict = build_verdict(
        symbol="SPX",
        selected_date="2026-08-25",
        events=events,
        sepa_candidates=[],
        mom_rows=[],
        iv_row=None,
        terrain={
            "regime": "range",
            "spot": 5000.0,
            "expected_close": 5010.0,
            "trade_date": "2026-08-25",
        },
        gex_latest=None,
        forecast_latest=None,
    )
    assert "Fed decision" in verdict["risk"]["text"]
    assert verdict["narrative"]["lamp"] == "green"


def test_build_verdict_sepa_opportunity() -> None:
    sepa = [
        {
            "symbol": "AAPL",
            "path": "SETUP",
            "grade": "A",
            "trade_date": "2026-08-25",
        }
    ]
    verdict = build_verdict(
        symbol="SPX",
        selected_date="2026-08-25",
        events=[],
        sepa_candidates=sepa,
        mom_rows=[],
        iv_row=None,
        terrain=None,
        gex_latest=None,
        forecast_latest={"regime": "transition", "expected_close": 5000.0, "trade_date": "2026-08-25"},
    )
    assert "AAPL" in verdict["opportunity"]["text"]
    assert verdict["action_hint"]["label"] == "View opportunity"


def test_build_verdict_regime_meta() -> None:
    verdict = build_verdict(
        symbol="SPX",
        selected_date="2026-08-25",
        events=[],
        sepa_candidates=[],
        mom_rows=[],
        iv_row=None,
        terrain={
            "regime": "range",
            "spot": 5000.0,
            "expected_close": 5010.0,
            "trade_date": "2026-08-25",
        },
        gex_latest=None,
        forecast_latest=None,
        regime_context={
            "lookback_days": 60,
            "current_regime": {
                "regime": "range",
                "path_hit_rate": 0.62,
                "sample_n": 18,
            },
        },
    )
    assert verdict["narrative"]["meta"] is not None
    assert "62%" in verdict["narrative"]["meta"]


# ─── research-loop-automation A4: narrative sign, symbol-scoped opportunity, honest tape ───

from bifrost_research.engines.brief.synth import _spot_vs_close, sentiment_card_verdict  # noqa: E402


def test_spot_vs_close_word_and_sign_describe_the_same_gap() -> None:
    above = _spot_vs_close(230.36, 225.40)
    assert "above" in above and "+2.20%" in above
    below = _spot_vs_close(220.00, 225.40)
    assert "below" in below and "-2.40%" in below


def _verdict_for(symbol: str, sepa: list[dict], mom: list[dict] | None = None) -> dict:
    return build_verdict(
        symbol=symbol,
        selected_date="2026-09-04",
        events=[],
        sepa_candidates=sepa,
        mom_rows=mom or [],
        iv_row=None,
        terrain={"regime": "range", "spot": 230.36, "expected_close": 225.4, "trade_date": "2026-09-04"},
        gex_latest=None,
        forecast_latest=None,
    )


FIVE = {"symbol": "FIVE", "path": "SETUP", "grade": "B", "sepa_score": 73.8, "trade_date": "2026-09-04"}
NVDA = {"symbol": "NVDA", "path": "PIVOT", "grade": "A", "sepa_score": 81.7, "trade_date": "2026-09-04"}


def test_opportunity_is_the_symbols_own_setup_when_it_has_one() -> None:
    v = _verdict_for("NVDA", [FIVE, NVDA])
    assert v["opportunity"]["text"].startswith("SEPA NVDA PIVOT · grade A · score 82")
    assert v["opportunity"]["lamp"] == "green"
    assert v["action_hint"]["label"] == "View opportunity"


def test_a_single_name_without_a_setup_says_so_instead_of_borrowing_another_ticker() -> None:
    v = _verdict_for("NVDA", [FIVE])
    assert v["opportunity"]["text"] == "No SEPA setup for NVDA · top today: SEPA FIVE SETUP · grade B · score 74"
    assert v["opportunity"]["lamp"] == "gray"
    assert v["action_hint"]["label"] == "Open narrative"
    none = _verdict_for("NVDA", [])
    assert none["opportunity"]["text"] == "No SEPA / Momentum opportunity for NVDA today"


def test_market_wide_symbol_still_reads_the_markets_best_setup() -> None:
    v = _verdict_for("SPY", [FIVE])
    assert v["opportunity"]["text"].startswith("SEPA FIVE SETUP")
    assert v["action_hint"]["label"] == "View opportunity"


def test_sentiment_card_is_honest_about_the_tape() -> None:
    proxy = {"trade_date": "2026-09-03", "sentiment_score": 0.0, "data_source": "option_snapshot_aggregates"}
    assert sentiment_card_verdict(proxy, "SPY") == "No tape — OI proxy only, no verdict · date 2026-09-03"
    tape = {"trade_date": "2026-09-03", "sentiment_score": 45.2, "data_source": "option_trades_tape"}
    assert sentiment_card_verdict(tape, "SPY") == "Net bias +45 (tape) · date 2026-09-03"
    assert sentiment_card_verdict(None, "SPY") == "No sentiment for SPY"
