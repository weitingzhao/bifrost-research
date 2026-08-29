"""Unit tests for Analyze Waves B.2 / C / E.3 / F — similar-regime, playbook, settlement."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import get_args

import pytest

from bifrost_research.api.playbook import dominant_scenario_suffix, evaluate_dominant_hit
from bifrost_research.api.similar_regime import Lens, parse_numeric_lens_value
from bifrost_research.api.wave4 import _enrich_settlement_row, _extract_hourly_realized
from bifrost_research.engines.forecast.playbook import (
    ScenarioProbabilities,
    evaluate_playbook_triggers,
)
from fastapi import HTTPException


def test_similar_regime_lens_includes_b2_and_f1() -> None:
    lenses = set(get_args(Lens))
    assert lenses == {
        "vrp",
        "iv_rank",
        "term_slope",
        "pin_distance",
        "gex_notional",
        "regime",
    }


def test_evaluate_playbook_triggers_snapshot() -> None:
    at = datetime(2024, 6, 3, 14, 30, tzinfo=timezone.utc)
    events = evaluate_playbook_triggers(
        symbol="qqq",
        trade_date=date(2024, 6, 3),
        trigger_at=at,
        scenarios=ScenarioProbabilities(0.55, 0.2, 0.15, 0.1),
        regime="range",
    )
    keys = {e["scenario_key"] for e in events}
    assert "dominant:rangy" in keys
    assert "rangy" in keys  # above 0.40 threshold
    assert all(e["symbol"] == "QQQ" for e in events)
    assert all(e["trigger_at"] == at for e in events)


def test_evaluate_playbook_triggers_dominant_change_and_cross() -> None:
    at = datetime(2024, 6, 3, 15, 0, tzinfo=timezone.utc)
    events = evaluate_playbook_triggers(
        symbol="SPY",
        trade_date=date(2024, 6, 3),
        trigger_at=at,
        scenarios=ScenarioProbabilities(0.15, 0.55, 0.2, 0.1),
        regime="trending",
        prev_dominant="rangy",
        prev_probs={"rangy": 0.5, "bull": 0.2, "bear": 0.2, "squeeze": 0.1},
    )
    keys = {e["scenario_key"] for e in events}
    assert "dominant:bull" in keys
    # bull crossed up past 0.40; rangy crossed down
    assert "bull" in keys
    assert "rangy" in keys
    bull_ev = next(e for e in events if e["scenario_key"] == "bull")
    assert bull_ev["satisfied"] is True
    rangy_ev = next(e for e in events if e["scenario_key"] == "rangy")
    assert rangy_ev["satisfied"] is False


def test_hit_rate_response_shape_helper() -> None:
    """Document expected hit-rate payload keys (handler is DB-backed)."""
    expected = {
        "symbol",
        "lookback_days",
        "session_count",
        "path_hit_rate",
        "avg_close_miss_pct",
        "direction_hit_rate",
        "rows",
    }
    # Shape contract used by FE — keep in sync with wave4.forecast_hit_rate
    sample = {
        "symbol": "QQQ",
        "lookback_days": 30,
        "session_count": 0,
        "path_hit_rate": 0.0,
        "avg_close_miss_pct": 0.0,
        "direction_hit_rate": None,
        "rows": [],
    }
    assert set(sample) == expected


def test_parse_numeric_lens_value_accepts_string_float() -> None:
    assert parse_numeric_lens_value("72.5") == 72.5
    assert parse_numeric_lens_value(" 0.01 ") == 0.01


def test_parse_numeric_lens_value_rejects_invalid() -> None:
    with pytest.raises(HTTPException) as exc:
        parse_numeric_lens_value("trending")
    assert exc.value.status_code == 400


def test_dominant_scenario_suffix_and_hit_rules() -> None:
    assert dominant_scenario_suffix("dominant:bull") == "bull"
    assert dominant_scenario_suffix("dominant:rangy") == "rangy"
    assert dominant_scenario_suffix("bull") is None
    assert evaluate_dominant_hit("bull", 0.05) is True
    assert evaluate_dominant_hit("bull", -0.01) is False
    assert evaluate_dominant_hit("bear", -0.02) is True
    assert evaluate_dominant_hit("rangy", 0.015) is True
    assert evaluate_dominant_hit("rangy", 0.03) is False
    assert evaluate_dominant_hit("squeeze", 0.01) is True
    assert evaluate_dominant_hit("squeeze", 0.02) is False


def test_extract_hourly_realized_from_stats_json() -> None:
    stats = {"hourly_close": [{"hour_et": 10, "close": 450.1}]}
    assert _extract_hourly_realized(stats) == [{"hour_et": 10, "close": 450.1}]
    assert _extract_hourly_realized({}) is None
    assert _extract_hourly_realized(None) is None


def test_enrich_settlement_row_adds_hourly_realized() -> None:
    row = {
        "stats_json": {"hourly_close": [{"hour_et": 15, "close": 100.0}]},
        "hourly_json": [{"hour_et": 15, "path_call": "hold"}],
    }
    enriched = _enrich_settlement_row(row)
    assert enriched["hourly_realized"] == [{"hour_et": 15, "close": 100.0}]
    assert enriched["hourly_json"] == [{"hour_et": 15, "path_call": "hold"}]
    missing = _enrich_settlement_row({"stats_json": {}})
    assert missing["hourly_realized"] is None
