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
