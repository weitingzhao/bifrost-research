"""research-loop-automation D2 — one digest per trading day, facts by code, prose optional."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from bifrost_research.copilot.agents import daily_digest as DD

DAY = date(2026, 9, 7)
NOW = datetime(2026, 9, 7, 11, 30, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=3)).isoformat()
OLD = (NOW - timedelta(days=3)).isoformat()


def _facts(**over: Any) -> dict[str, Any]:
    f = DD.empty_facts(DAY)
    f.update(
        {
            "loop": {
                "objectives": [{"id": "obj_stock", "title": "Daily Loop Stock Explorer"}],
                "runs": [{"id": "run_1", "objective_id": "obj_stock", "status": "awaiting_approval", "started_at": FRESH}],
                "pending": {"candidate_batch": 24, "policy_suggestion": 4},
                "trust": {"l0": False, "reason": "not L0"},
            },
            "candidates": [{"symbol": "WT", "source": "harness", "score": 82.1, "status": "open"}],
            "batches": [
                {"run_id": "run_1", "objective_id": "obj_stock", "objective_title": "Daily Loop Stock Explorer", "status": "awaiting_approval", "started_at": FRESH, "candidates": ["WT", "LPG"], "draft_ids": ["draft_1"], "dissent": 1, "persona_mode": "agent", "plan_generated_by": "llm"}
            ],
            "dissents": [
                {"symbol": "WT", "run_id": "run_1", "objective_title": "Daily Loop Stock Explorer", "net_stance": "dissent", "blocked_by_validate": False, "judges": ["deepseek-chat: support", "gpt-4o-mini: oppose"], "wrong_if": ["close below the 50-day"]}
            ],
            "resolutions": [
                {"id": "hyp_lpg", "title": "LPG breakout", "status": "validated", "symbols": ["LPG"], "decision": "validated", "excess": 0.0343, "horizon_days": 20, "resolved_at": FRESH, "by_rule": True}
            ],
            "active_hypotheses": {"count": 36, "titles": ["LPG breakout"]},
            "holdings": {"status": "ok", "symbols": ["NVDA"]},
            "symbols": ["NVDA", "WT"],
            "exhibits": {
                "NVDA": [
                    {"lens": "iv_rank", "band": "cold", "value": 18.7, "means": "Implied vol near its 1y low — premium is cheap; long-premium bias.", "as_of": "2026-09-04", "freshness": "fresh"},
                    {"lens": "vrp", "band": "lean_cold", "value": 20.7, "means": "buying vol is cheap", "as_of": "2026-09-04", "freshness": "fresh"},
                ],
                "WT": [{"lens": "iv_rank", "band": None, "value": None, "means": None, "as_of": None, "freshness": "missing"}],
            },
            "discoveries": [{"source": "sepa", "symbol": "PAYS", "sepa_score": 82.75, "grade": "A", "stage": "STAGE_2"}],
        }
    )
    f.update(over)
    return f


def test_markdown_carries_every_section_from_the_facts() -> None:
    md = DD.compose_markdown(_facts())
    assert md.startswith("## Daily digest · 2026-09-07")
    assert "1 active objective(s) · 1 run(s) since yesterday (1 awaiting approval)" in md
    assert "24 candidate batch(es), 4 policy suggestion(s) · trust not L0 (not L0)" in md
    assert "1 candidate(s) proposed (WT) · 1 hypothesis resolution(s) · 1 dissent(s) · 36 active hypothesis(es)" in md
    assert "- **NVDA**: iv_rank cold (19) · vrp lean_cold (21) — Implied vol near its 1y low" in md
    assert "- No lens readings yet (no option / terrain data): WT" in md
    assert "Daily Loop Stock Explorer (`run_1`, awaiting_approval): WT, LPG — 1 dissent(s)" in md
    assert "**WT** (Daily Loop Stock Explorer): deepseek-chat: support; gpt-4o-mini: oppose · wrong if: close below the 50-day" in md
    assert "LPG breakout → **validated** (by outcome rule excess +3.43%; LPG)" in md
    assert "SEPA PAYS score 82.75 grade A stage STAGE_2" in md
    assert md.rstrip().endswith(DD.ADVISORY)


def test_an_empty_day_still_reads_honestly() -> None:
    md = DD.compose_markdown(DD.empty_facts(DAY))
    assert "No holdings and no new candidates" in md
    assert "No run since yesterday" in md
    assert "Resolutions since yesterday\n- None" in md
    assert "Holdings not applied (unavailable)" in md


def test_prose_falls_back_to_the_heuristic_when_the_model_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    payload = DD.build_payload(_facts())  # no key: the endpoint table says so, nothing is called
    assert payload["model"] == "heuristic" and payload["llm_enriched"] is False
    assert payload["markdown"] == payload["markdown_heuristic"]
    assert payload["prose"]["error"] == "DEEPSEEK_API_KEY not configured"

    seen: list[dict[str, Any]] = []

    def _writer(model: str, messages: list[dict[str, str]], timeout_seconds: float = 0) -> tuple[str | None, dict[str, Any]]:
        seen.append({"model": model, "system": messages[0]["content"], "user": messages[1]["content"]})
        return "## Daily digest · 2026-09-07\nWT split the judges.", {"model": model, "cost_usd": 0.0004, "input_tokens": 900, "output_tokens": 120}

    monkeypatch.setattr(DD, "_chat_completion", _writer)
    payload = DD.build_payload(_facts())
    assert payload["llm_enriched"] is True and payload["model"] == "deepseek-chat"
    assert payload["markdown"].endswith(DD.ADVISORY)  # the advisory is appended when the model drops it
    assert payload["prose"]["cost_usd"] == 0.0004
    assert seen[0]["model"] == "deepseek-chat" and "FACTS JSON" in seen[0]["user"] and "never add a number" in seen[0]["system"]
    assert payload["batches"][0]["run_id"] == "run_1" and payload["dissents"][0]["symbol"] == "WT"


def test_dry_run_posts_nothing(monkeypatch, capsys) -> None:
    out = DD.run_daily_digest(dry_run=True, day=DAY)
    assert out["ok"] is True and out["dry_run"] is True and out["scope"] == "digest:2026-09-07"
    assert out["payload"]["model"] == "heuristic"
    assert "Daily digest" in capsys.readouterr().out


def test_one_digest_per_day_unless_forced(monkeypatch) -> None:
    inserted: list[dict[str, Any]] = []
    existing = {"id": "draft_digest", "kind": "daily_digest", "status": "pending"}

    monkeypatch.setattr(DD, "existing_digest", lambda conn, day: existing)
    monkeypatch.setattr(DD, "gather_facts", lambda conn, day, now=None: _facts())
    monkeypatch.setattr(DD.action_repo, "insert_action", lambda conn, **kw: {"id": "act_1", **kw})
    monkeypatch.setattr(DD.draft_repo, "insert_draft", lambda conn, **kw: inserted.append(kw) or {"id": f"draft_{len(inserted)}", **kw})

    skipped = DD.run_daily_digest(object(), day=DAY, dry_run=False)
    assert skipped == {"ok": True, "skipped": True, "reason": "digest already posted for the day", "draft_id": "draft_digest", "day": "2026-09-07"}
    assert inserted == []

    done = DD.run_daily_digest(object(), day=DAY, dry_run=False, force=True, use_llm=False)
    assert done["ok"] is True and done["draft_id"] == "draft_1" and done["replaced"] is True
    assert done["symbols"] == 2 and done["batches"] == 1 and done["dissents"] == 1 and done["resolutions"] == 1
    draft = inserted[0]
    assert draft["kind"] == "daily_digest" and draft["scope"] == "digest:2026-09-07" and draft["generated_by"] == "digest_agent"
    assert draft["payload"]["title"] == "Daily digest 2026-09-07" and draft["payload"]["advisory"] == DD.ADVISORY

    monkeypatch.setattr(DD, "existing_digest", lambda conn, day: None)
    first = DD.run_daily_digest(object(), day=DAY, dry_run=False, use_llm=False)
    assert first["replaced"] is False and first["draft_id"] == "draft_2"


def test_gather_reads_runs_candidates_and_resolutions_since_yesterday(monkeypatch) -> None:
    monkeypatch.setattr(DD.obj_repo, "list_objectives", lambda conn, **kw: [{"id": "obj_stock", "title": "Daily Loop Stock Explorer"}])
    monkeypatch.setattr(
        DD.obj_repo,
        "list_runs",
        lambda conn, **kw: [
            {"id": "run_new", "objective_id": "obj_stock", "status": "awaiting_approval", "started_at": FRESH},
            {"id": "run_repeat", "objective_id": "obj_stock", "status": "awaiting_approval", "started_at": FRESH},
            {"id": "run_old", "objective_id": "obj_stock", "status": "completed", "started_at": OLD},
        ],
    )
    monkeypatch.setattr(DD.draft_repo, "count_pending", lambda conn, kind=None: {"candidate_batch": 2}.get(kind, 0))
    monkeypatch.setattr("bifrost_research.copilot.harness.batch_orchestrate.trust_status", lambda: {"l0": False, "reason": "not L0"})
    monkeypatch.setattr(
        DD.cand_repo,
        "list_candidates",
        lambda conn, **kw: [
            {"symbol": "wt", "source": "harness", "score": 82.1, "status": "open", "created_at": FRESH},
            {"symbol": "OLD", "source": "harness", "score": 50.0, "status": "open", "created_at": OLD},
        ],
    )
    monkeypatch.setattr(
        DD.run_digest,
        "digest_run",
        lambda conn, rid: {
            "run": {"id": rid, "objective_id": "obj_stock", "objective_title": "Daily Loop Stock Explorer", "status": "awaiting_approval", "started_at": FRESH},
            "candidates": [
                {
                    "symbol": "WT",
                    "net_stance": "dissent",
                    "blocked_by_validate": False,
                    "verdicts": [
                        {"model": "deepseek-chat", "agent": "analyze", "stance": "support"},
                        {"model": "deepseek-chat", "agent": "verdict", "stance": "support"},
                        {"model": "gpt-4o-mini", "agent": "analyze", "stance": "caution"},
                        {"model": "gpt-4o-mini", "agent": "portfolio", "stance": "oppose"},
                    ],
                    "report": {"wrong_if": ["close below the 50-day", "grade drops", "a third reason"]},
                },
                {"symbol": "LPG", "net_stance": "support", "blocked_by_validate": False, "verdicts": [], "report": None},
            ],
            "drafts": [{"id": "draft_1", "kind": "candidate_batch", "status": "pending"}],
            "persona": {"mode": "agent"},
            "plan": {"generated_by": "llm"},
        },
    )

    def _hyps(conn, status=None, **kw):
        if status == "validated":
            return [{"id": "hyp_lpg", "title": "LPG breakout", "symbols": ["LPG"], "resolution_json": {"resolved_at": FRESH, "decision": "validated", "excess": 0.0343, "horizon_days": 20}}]
        if status == "rejected":
            return [{"id": "hyp_old", "title": "stale", "symbols": ["X"], "resolution_json": {"resolved_at": OLD, "decision": "rejected"}}]
        return [{"id": "h1", "title": "one"}, {"id": "h2", "title": "two"}]

    monkeypatch.setattr(DD.hyp_repo, "list_hypotheses", _hyps)
    monkeypatch.setattr(DD, "_holdings", lambda: (["NVDA"], "ok"))
    monkeypatch.setattr(DD, "_symbol_exhibits", lambda conn, symbols: {s: [{"lens": "iv_rank", "band": "cold", "value": 18.7, "means": "cheap", "as_of": "2026-09-04", "freshness": "fresh"}] for s in symbols})
    monkeypatch.setattr(DD, "gather_discoveries", lambda conn, limit=5: [])

    facts = DD.gather_facts(object(), day=DAY, now=NOW)
    assert [r["id"] for r in facts["loop"]["runs"]] == ["run_new", "run_repeat"]
    # the identical re-run folds into the newest batch; the split name is listed once
    assert len(facts["batches"]) == 1 and facts["batches"][0]["repeats"] == 2
    assert facts["batches"][0]["run_ids"] == ["run_new", "run_repeat"] and len(facts["dissents"]) == 1
    assert [c["symbol"] for c in facts["candidates"]] == ["WT"]
    assert facts["symbols"] == ["NVDA", "WT"]  # holdings first, then the new candidates, de-duplicated
    assert facts["batches"][0]["candidates"] == ["WT", "LPG"] and facts["batches"][0]["dissent"] == 1
    # one entry per judge model: its verdict agent's stance, else its most severe agent; two wrong-ifs at most
    assert facts["dissents"][0]["judges"] == ["deepseek-chat: support", "gpt-4o-mini: oppose"]
    assert facts["dissents"][0]["wrong_if"] == ["close below the 50-day", "grade drops"]
    assert [r["title"] for r in facts["resolutions"]] == ["LPG breakout"] and facts["resolutions"][0]["by_rule"] is True
    assert facts["active_hypotheses"]["count"] == 2 and facts["loop"]["pending"]["candidate_batch"] == 2
    assert facts["exhibits"]["NVDA"][0]["band"] == "cold"
