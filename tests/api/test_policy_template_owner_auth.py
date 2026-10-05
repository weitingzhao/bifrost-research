"""Policy template writes need a research user.

A template is the Loop's strategy as data: saving, editing or deleting one
changes what the next objective is created from. The frontend's
createPolicyTemplate / patchPolicyTemplate / deletePolicyTemplate send the
research bearer, and nothing else calls them over HTTP. ``POST /validate`` is a
dry run that writes nothing and stays open.

The template's ``owner_id`` used to default to the literal ``"owner"``; a body
that does not name one is now owned by the resolved research user.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map

_BASE = "/research/policy-templates"
_GATED = (
    ("POST", _BASE, {"name": "T", "policy_json": {}}),
    ("PATCH", f"{_BASE}/tpl_1", {"name": "T2"}),
    ("DELETE", f"{_BASE}/tpl_1", None),
)
_ALICE = {"Authorization": "Bearer tok_alice"}


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
        self.created: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str]] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    def _create(conn: Any, **kw: Any) -> dict[str, Any]:
        e.created.append(kw)
        return {"id": "tpl_new", **kw}

    def _update(conn: Any, template_id: str, **kw: Any) -> dict[str, Any]:
        e.writes.append(("patch", template_id))
        return {"id": template_id, "policy_json": {}, **kw}

    def _delete(conn: Any, template_id: str) -> bool:
        e.writes.append(("delete", template_id))
        return True

    mod = "bifrost_research.api.policy_template"
    monkeypatch.setattr(f"{mod}.connect", _connect)
    monkeypatch.setattr(f"{mod}.tpl_repo.create_template", _create)
    monkeypatch.setattr(f"{mod}.tpl_repo.update_template", _update)
    monkeypatch.setattr(f"{mod}.tpl_repo.delete_template", _delete)
    monkeypatch.setattr(
        f"{mod}.tpl_repo.get_template", lambda conn, tid: {"id": tid, "policy_json": {}}
    )
    monkeypatch.setattr(f"{mod}.tpl_repo.count_objectives_using", lambda conn, tid: 0)
    monkeypatch.setattr(f"{mod}.tpl_repo.validate_policy", lambda policy: (policy, []))
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize(("method", "path", "body"), _GATED)
def test_anonymous_call_is_refused(
    client: TestClient, env: _Env, auth_on: None, method: str, path: str, body: Any
) -> None:
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert env.created == [] and env.writes == []


@pytest.mark.parametrize(("method", "path", "body"), _GATED)
def test_unknown_token_is_refused(
    client: TestClient, env: _Env, auth_on: None, method: str, path: str, body: Any
) -> None:
    resp = client.request(method, path, json=body, headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert env.created == [] and env.writes == []


def test_validate_stays_open(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(f"{_BASE}/validate", json={"policy_json": {}})
    assert resp.status_code == 200, resp.text
    assert env.created == [] and env.writes == []


def test_research_user_creates_and_owns_the_template(
    client: TestClient, env: _Env, auth_on: None
) -> None:
    resp = client.post(_BASE, json={"name": "T", "policy_json": {}}, headers=_ALICE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] == "tpl_new"
    # No owner_id in the body: the resolved user owns it, not "owner".
    assert env.created[0]["owner_id"] == "alice"


def test_create_keeps_body_owner_id(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        _BASE, json={"name": "T", "policy_json": {}, "owner_id": "owner"}, headers=_ALICE
    )
    assert resp.status_code == 200, resp.text
    assert env.created[0]["owner_id"] == "owner"


def test_research_user_can_patch(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.patch(f"{_BASE}/tpl_1", json={"name": "T2"}, headers=_ALICE)
    assert resp.status_code == 200, resp.text
    assert env.writes == [("patch", "tpl_1")]


def test_research_user_can_delete(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.delete(f"{_BASE}/tpl_1", headers=_ALICE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"id": "tpl_1", "deleted": True}
    assert env.writes == [("delete", "tpl_1")]
