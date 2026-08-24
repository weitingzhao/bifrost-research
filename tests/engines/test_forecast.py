"""Wave 4.2 AI Forecast Engine tests (offline heuristic LLM)."""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.forecast.llm import HeuristicLLMProvider, get_default_provider
from bifrost_research.engines.forecast.playbook import (
    build_forecast_session,
    recommend_option_structures,
    scenario_probabilities_from_terrain,
)
from bifrost_research.engines.forecast.terrain import compute_market_terrain


def _terrain():
    return compute_market_terrain(
        "QQQ",
        date(2024, 6, 3),
        spot=450.0,
        gex={
            "zero_gamma": 448.0,
            "major_call_wall": 460.0,
            "major_put_wall": 440.0,
            "total_net_gex": 5e8,
        },
        momentum={"score": 72, "path": "EXT", "crash": 75},
        iv={"iv_percentile_1y": 35.0},
    )


def test_heuristic_provider_offline() -> None:
    p = HeuristicLLMProvider()
    text = p.complete("trending bull squeeze playbook")
    assert "heuristic" in text.lower() or "Advisory" in text
    assert get_default_provider().name == "heuristic"


def test_scenarios_sum_to_one() -> None:
    probs = scenario_probabilities_from_terrain(_terrain()).normalized()
    total = probs.rangy + probs.bull + probs.bear + probs.squeeze
    assert abs(total - 1.0) < 1e-6


def test_forecast_session_hourly_and_structures() -> None:
    session = build_forecast_session(_terrain(), llm=HeuristicLLMProvider(), enrich=True)
    assert session.session_id
    assert len(session.hourly) == 6
    assert all(h.hour_et >= 10 for h in session.hourly)
    names = {s.structure for s in session.structures}
    assert "Iron Condor" in names
    assert "Butterfly" in names
    assert "Bull Call Vertical" in names
    assert "D10" in session.advisory
    assert session.llm_provider == "heuristic"
    d = session.to_dict()
    assert "scenarios" in d
    assert abs(sum(d["scenarios"].values()) - 1.0) < 1e-6


def test_option_structure_pop_bounds() -> None:
    from bifrost_research.engines.forecast.playbook import ScenarioProbabilities

    recs = recommend_option_structures(
        spot=100.0,
        scenarios=ScenarioProbabilities(0.5, 0.2, 0.2, 0.1),
        gamma_zone_low=98.0,
        gamma_zone_high=102.0,
        regime="range",
    )
    for r in recs:
        assert 0.0 <= r.pop <= 1.0
        assert r.cvar <= 0.0
