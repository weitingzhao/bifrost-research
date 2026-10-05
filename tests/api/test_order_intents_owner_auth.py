"""Order-intent drafts need a research user to create or expire.

Advisory only (D10 BLOCKED): an order intent is a Research draft, never an order.
It still lands in the Inbox and the action ledger, and expiring one closes it for
good, so an anonymous caller must not reach either write. Nothing calls these over
HTTP today; the MCP tool writes the same rows in-process.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map

_INTENT = {
    "intent": {"hypothesis_id": "hyp_1", "strategy_template": "short_strangle_30d"},
}


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard",
        lambda: None,
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RESEARCH_USERS", "alice:tok_alice")
    monkeypatch.delenv("RESEARCH_API_TOKEN", raising=False)
    token_to_owner_map.cache_clear()
    yield
    token_to_owner_map.cache_clear()


class _Env:
    def __init__(self) -> None:
        self.connects = 0
        self.drafts: list[dict[str, Any]] = []
        self.status_updates: list[dict[str, Any]] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    def _insert_draft(conn: Any, **k: Any) -> dict[str, Any]:
        e.drafts.append(k)
        return {"id": "aid_new", **k}

    def _update(conn: Any, draft_id: str, *, status: str) -> dict[str, Any]:
        e.status_updates.append({"draft_id": draft_id, "status": status})
        return {"id": draft_id, "kind": "order_intent", "status": status}

    monkeypatch.setattr("bifrost_research.api.order_intents.connect", _connect)
    monkeypatch.setattr(
        "bifrost_research.api.order_intents.action_repo.insert_action",
        lambda conn, **k: {"id": "aal_new", **k},
    )
    monkeypatch.setattr(
        "bifrost_research.api.order_intents.draft_repo.insert_draft", _insert_draft
    )
    monkeypatch.setattr(
        "bifrost_research.api.order_intents.draft_repo.get_draft",
        lambda conn, draft_id: {"id": draft_id, "kind": "order_intent", "status": "pending"},
    )
    monkeypatch.setattr(
        "bifrost_research.api.order_intents.draft_repo.update_draft_status", _update
    )

    # A pending intent expires through the shared path (payload.expired + action row).
    def _expire_ids(conn: Any, ids: list[str], *, reason: str, by: str) -> dict[str, Any]:
        for did in ids:
            e.status_updates.append({"draft_id": did, "status": "expired", "reason": reason, "by": by})
        return {"expired": len(ids), "actions_expired": 0, "ids": ids}

    monkeypatch.setattr(
        "bifrost_research.api.order_intents.draft_expiry.expire_ids", _expire_ids
    )
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


_CALLS = (
    ("/research/order-intents", _INTENT),
    ("/research/order-intents/aid_1/expire", None),
)


@pytest.mark.parametrize(("path", "body"), _CALLS)
def test_anonymous_write_is_refused(
    client: TestClient, env: _Env, auth_on: None, path: str, body: Any
) -> None:
    resp = client.post(path, json=body)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert env.drafts == [] and env.status_updates == []


@pytest.mark.parametrize(("path", "body"), _CALLS)
def test_unknown_token_is_refused(
    client: TestClient, env: _Env, auth_on: None, path: str, body: Any
) -> None:
    resp = client.post(path, json=body, headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert env.drafts == [] and env.status_updates == []


def test_research_user_can_create(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/order-intents",
        json=_INTENT,
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["d10"] == "BLOCKED"
    assert data["draft"]["kind"] == "order_intent"
    # generated_by keeps its own default: it names the generator, not the user.
    assert env.drafts[0]["generated_by"] == "harness"


def test_research_user_can_expire(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/order-intents/aid_1/expire",
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert env.status_updates == [
        {"draft_id": "aid_1", "status": "expired", "reason": "manual", "by": "owner:alice"}
    ]
