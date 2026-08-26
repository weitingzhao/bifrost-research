"""Morning Prep agent unit tests — dry-run / heuristic (no live DB / LLM)."""

from __future__ import annotations

from bifrost_research.copilot.agents.morning_prep import (
    _heuristic_global_brief,
    _heuristic_hypothesis_brief,
    run_morning_prep,
)


def test_heuristic_hypothesis_brief_without_data() -> None:
    hyp = {"id": "h1", "title": "Test", "symbols": ["SPY"], "status": "active"}
    payload = _heuristic_hypothesis_brief(hyp, {"symbols": {}})
    assert payload["hypothesis_id"] == "h1"
    assert payload["model"] == "heuristic"
    assert "markdown" in payload
    assert len(payload["bullets"]) >= 2


def test_heuristic_global_brief_empty() -> None:
    payload = _heuristic_global_brief([])
    assert payload["create_hypothesis"] is False
    assert "Discoveries" in payload["markdown"] or "discoveries" in payload["markdown"].lower()


def test_morning_dry_run(monkeypatch) -> None:
    monkeypatch.setenv("BIFROST_MORNING_AGENT_DRY_RUN", "1")
    result = run_morning_prep(dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["count"] == 2
    kinds = {d["kind"] for d in result["drafts"]}
    assert kinds == {"morning_brief"}
    scopes = {d["scope"] for d in result["drafts"]}
    assert "global" in scopes
