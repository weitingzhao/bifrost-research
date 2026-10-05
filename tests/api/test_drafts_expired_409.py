"""An expired draft cannot be approved or dismissed (0.166.0, D1).

A tab opened before the sweep (or before a newer draft covered this one) still
shows the old card. Approving it would apply a stale verdict; dismissing a
candidate_batch would mark its pool rows ``dismissed``, which the decline gate
reads as the Owner's "no" (D8). Both answer 409 with a machine-readable body:
``detail.code == "draft_expired"`` and ``detail.reason``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


@pytest.fixture(autouse=True)
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RESEARCH_USERS", "alice:tok_alice")
    monkeypatch.delenv("RESEARCH_API_TOKEN", raising=False)
    token_to_owner_map.cache_clear()
    yield
    token_to_owner_map.cache_clear()


class _Env:
    def __init__(self) -> None:
        self.draft: dict[str, Any] = {}
        self.writes: list[str] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()
    monkeypatch.setattr("bifrost_research.api.agents.connect", lambda: MagicMock())
    monkeypatch.setattr("bifrost_research.api.agents.draft_repo.get_draft", lambda conn, did: e.draft)

    def _write(name: str):
        def _fn(*a: Any, **k: Any) -> dict[str, Any]:
            e.writes.append(name)
            return {"id": "x"}

        return _fn

    for target in (
        "bifrost_research.api.agents.draft_repo.update_draft_status",
        "bifrost_research.api.agents.action_repo.update_action_status",
        "bifrost_research.api.agents.action_repo.insert_action",
        "bifrost_research.api.agents.cand_repo.dismiss_candidate",
        "bifrost_research.api.agents.hyp_repo.patch_hypothesis",
    ):
        monkeypatch.setattr(target, _write(target.rsplit(".", 1)[-1]))
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


_AUTH = {"Authorization": "Bearer tok_alice"}

_SUPERSEDED = {
    "id": "drf_old",
    "kind": "candidate_batch",
    "status": "expired",
    "payload": {
        "objective_id": "obj_a",
        "items": [{"id": "c1", "symbol": "AAA"}],
        "expired": {"reason": "superseded", "by": "insert:harness", "at": "2026-10-05T21:30:00+00:00", "superseded_by": "drf_new"},
    },
    "linked_action_id": "aal_1",
    "expires_at": None,
}

_DUE = {
    "id": "drf_due",
    "kind": "eod_verdict",
    "status": "pending",
    "payload": {"hypothesis_id": "hyp_a", "proposed_status": "active"},
    "linked_action_id": "aal_2",
    "expires_at": "2026-01-02T21:00:00+00:00",
}


@pytest.mark.parametrize("verb", ["approve", "dismiss"])
def test_superseded_draft_is_409(client: TestClient, env: _Env, verb: str) -> None:
    env.draft = dict(_SUPERSEDED)
    resp = client.post(f"/research/drafts/drf_old/{verb}", headers=_AUTH)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "draft_expired"
    assert detail["reason"] == "superseded"
    assert detail["superseded_by"] == "drf_new"
    assert detail["draft_id"] == "drf_old"
    assert "expired" in detail["message"]
    # Nothing written — above all, no pool row dismissed (D8).
    assert env.writes == []


@pytest.mark.parametrize("verb", ["approve", "dismiss"])
def test_pending_past_its_clock_is_409(client: TestClient, env: _Env, verb: str) -> None:
    env.draft = dict(_DUE)
    resp = client.post(f"/research/drafts/drf_due/{verb}", headers=_AUTH)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "draft_expired"
    assert detail["reason"] == "due"
    assert detail["expires_at"] == "2026-01-02T21:00:00+00:00"
    assert env.writes == []


def test_other_closed_statuses_keep_their_message(client: TestClient, env: _Env) -> None:
    env.draft = {**_DUE, "status": "approved", "expires_at": None}
    resp = client.post("/research/drafts/drf_due/approve", headers=_AUTH)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "draft status is approved, expected pending"
