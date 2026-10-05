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


def test_eod_sweeps_expired_drafts_before_writing(monkeypatch) -> None:
    """D5: the EOD review (CronJob, Dagster asset and API all call run_eod_review)
    runs the draft sweep once, first, then writes the day's verdicts."""
    from bifrost_research.copilot.agents import eod_review as E

    order: list[str] = []
    monkeypatch.setattr(E.draft_repo, "expire_due", lambda conn, **kw: order.append(f"expire:{kw.get('by')}") or {"ok": True, "expired": 3})
    monkeypatch.setattr(E, "resolve_active", lambda conn: order.append("resolve") or {"ok": True, "entries": []})
    monkeypatch.setattr(E.hyp_repo, "list_hypotheses", lambda conn, **kw: [{"id": "h1", "title": "T", "symbols": []}])
    monkeypatch.setattr(E, "gather_symbol_context", lambda conn, symbols: {"symbols": {}})
    monkeypatch.setattr(E, "_optional_llm_enrich", lambda prompt, payload: payload)
    monkeypatch.setattr(E.action_repo, "insert_action", lambda conn, **kw: {"id": "aal_1"})
    monkeypatch.setattr(E.draft_repo, "insert_draft", lambda conn, **kw: order.append("insert") or {"id": "drf_1", **kw})

    out = run_eod_review(object(), dry_run=False)
    assert order == ["expire:eod_agent", "resolve", "insert"]
    assert out["expiry"] == {"ok": True, "expired": 3}


def test_eod_survives_a_failed_sweep(monkeypatch) -> None:
    from bifrost_research.copilot.agents import eod_review as E

    def _boom(conn, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(E.draft_repo, "expire_due", _boom)
    monkeypatch.setattr(E, "rollback_quietly", lambda conn: None)
    monkeypatch.setattr(E, "resolve_active", lambda conn: {"ok": True, "entries": []})
    monkeypatch.setattr(E.hyp_repo, "list_hypotheses", lambda conn, **kw: [])
    out = run_eod_review(object(), dry_run=False)
    assert out["ok"] is True
    assert out["expiry"]["ok"] is False and "db down" in out["expiry"]["error"]
