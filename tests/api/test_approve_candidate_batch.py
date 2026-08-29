"""Approve candidate_batch → lightweight hypothesis + promote_candidate."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard",
        lambda: None,
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


class _Env:
    def __init__(self) -> None:
        self.draft: dict[str, Any] | None = None
        self.candidates: dict[str, dict[str, Any]] = {}
        self.created_hyps: list[dict[str, Any]] = []
        self.promote_calls: list[dict[str, Any]] = []
        self.update_draft_captured: dict[str, Any] = {}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()
    fake_conn = MagicMock()
    monkeypatch.setattr("bifrost_research.api.agents.connect", lambda: fake_conn)

    monkeypatch.setattr(
        "bifrost_research.api.agents.draft_repo.get_draft",
        lambda conn, draft_id: e.draft,
    )

    def _update_draft(conn, draft_id, *, status):
        e.update_draft_captured = {"draft_id": draft_id, "status": status}
        return {"id": draft_id, "status": status, "kind": "candidate_batch"}

    monkeypatch.setattr(
        "bifrost_research.api.agents.draft_repo.update_draft_status", _update_draft
    )
    monkeypatch.setattr(
        "bifrost_research.api.agents.cand_repo.get_candidate",
        lambda conn, cid: e.candidates.get(cid),
    )

    def _promote(conn, cid, *, hypothesis_id):
        e.promote_calls.append({"candidate_id": cid, "hypothesis_id": hypothesis_id})
        row = e.candidates.get(cid)
        if row is None or row.get("status") != "open":
            return None
        updated = {**row, "status": "promoted", "hypothesis_id": hypothesis_id}
        e.candidates[cid] = updated
        return updated

    monkeypatch.setattr(
        "bifrost_research.api.agents.cand_repo.promote_candidate", _promote
    )

    def _create_hyp(conn, **kwargs):
        hid = f"hyp-{len(e.created_hyps) + 1}"
        row = {"id": hid, **kwargs}
        e.created_hyps.append(row)
        return row

    monkeypatch.setattr(
        "bifrost_research.api.agents.hyp_repo.create_hypothesis", _create_hyp
    )
    monkeypatch.setattr(
        "bifrost_research.api.agents.action_repo.insert_action",
        lambda conn, **k: {"id": "aal_new", **k},
    )
    monkeypatch.setattr(
        "bifrost_research.api.agents.action_repo.update_action_status",
        lambda conn, action_id, **k: {"id": action_id, **k},
    )
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_approve_candidate_batch_promotes_and_creates_hypothesis(
    client: TestClient, env: _Env
) -> None:
    env.candidates["cand-aaa-1"] = {
        "id": "cand-aaa-1",
        "symbol": "AAA",
        "status": "open",
        "lens_snapshot": {"composite_score": 81.0, "lens_flags": {"iv_rank": "hot"}},
    }
    env.draft = {
        "id": "aid_cb_1",
        "kind": "candidate_batch",
        "status": "pending",
        "payload": {
            "title": "Morning IV",
            "description": "IV-hot watch",
            "run_id": "run-9",
            "items": [{"id": "cand-aaa-1", "symbol": "AAA", "score": 81.0}],
        },
        "linked_action_id": "aal_orig",
    }

    resp = client.post("/research/drafts/aid_cb_1/approve")
    assert resp.status_code == 200, resp.text
    executed = resp.json()["data"]["executed"]
    assert executed["kind"] == "candidate_batch"
    assert len(executed["hypotheses"]) == 1
    assert executed["hypotheses"][0]["title"] == "Morning IV · AAA"
    assert executed["hypotheses"][0]["symbols"] == ["AAA"]
    assert executed["promoted"][0]["status"] == "promoted"
    assert executed["promoted"][0]["hypothesis_id"] == executed["hypotheses"][0]["id"]
    assert env.promote_calls == [
        {"candidate_id": "cand-aaa-1", "hypothesis_id": executed["hypotheses"][0]["id"]}
    ]
    assert env.update_draft_captured["status"] == "approved"
    assert executed["skipped"] == []


def test_approve_candidate_batch_skips_missing_and_non_open(
    client: TestClient, env: _Env
) -> None:
    env.candidates["cand-gone"] = None  # type: ignore[assignment]
    env.candidates["cand-closed"] = {
        "id": "cand-closed",
        "symbol": "BBB",
        "status": "dismissed",
        "lens_snapshot": {},
    }
    env.draft = {
        "id": "aid_cb_2",
        "kind": "candidate_batch",
        "status": "pending",
        "payload": {
            "title": "Batch",
            "items": [
                {"id": "cand-missing", "symbol": "ZZZ"},
                {"id": "cand-closed", "symbol": "BBB"},
            ],
        },
    }

    resp = client.post("/research/drafts/aid_cb_2/approve")
    assert resp.status_code == 200, resp.text
    executed = resp.json()["data"]["executed"]
    assert executed["promoted"] == []
    assert executed["hypotheses"] == []
    reasons = {s["id"]: s["reason"] for s in executed["skipped"]}
    assert reasons["cand-missing"] == "not_found"
    assert reasons["cand-closed"].startswith("not_open:")
    assert env.created_hyps == []
    assert env.update_draft_captured["status"] == "approved"
