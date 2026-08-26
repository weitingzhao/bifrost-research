"""EOD Review agent unit tests — dry-run / heuristic (no live DB / LLM)."""

from __future__ import annotations

from bifrost_research.copilot.agents.eod_review import (
    _heuristic_verdict,
    run_eod_review,
)


def test_heuristic_verdict_no_material_change() -> None:
    hyp = {"id": "h1", "title": "Test", "symbols": ["SPY"]}
    payload = _heuristic_verdict(hyp, {"symbols": {}})
    assert payload["proposed_status"] == "active"
    assert "keep" in payload["rationale"].lower() or "no material" in payload["rationale"].lower()
    assert payload["model"] == "heuristic"


def test_heuristic_verdict_with_extreme_vrp() -> None:
    hyp = {"id": "h1", "title": "Test", "symbols": ["NVDA"]}
    ctx = {
        "symbols": {
            "NVDA": {
                "vrp": {"vrp_pct_252d": 95.0, "vrp_20d": 0.1},
                "events": [{"title": "earnings"}],
                "regime": {"regime": "squeeze"},
            }
        }
    }
    payload = _heuristic_verdict(hyp, ctx)
    assert payload["proposed_status"] == "active"
    assert payload["notes"]


def test_eod_dry_run(monkeypatch) -> None:
    monkeypatch.setenv("BIFROST_EOD_AGENT_DRY_RUN", "1")
    result = run_eod_review(dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["count"] == 1
    assert result["drafts"][0]["kind"] == "eod_verdict"
    assert "proposed_status" in result["drafts"][0]["payload"]
