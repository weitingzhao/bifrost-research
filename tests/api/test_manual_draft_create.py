"""Wave X (Loop Usability) — POST /research/drafts owner-manual create."""

from __future__ import annotations

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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
    fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("bifrost_research.api.agents.connect", lambda: fake_conn)

    monkeypatch.setattr(
        "bifrost_research.api.agents.action_repo.insert_action",
        lambda conn, **kwargs: {"id": "aal_test_1", **kwargs, "status": kwargs.get("status", "proposed")},
    )

    captured: dict[str, object] = {}

    def _fake_insert_draft(conn, **kwargs):
        captured.update(kwargs)
        return {
            "id": "aid_test_1",
            "kind": kwargs["kind"],
            "payload": kwargs["payload"],
            "scope": kwargs["scope"],
            "generated_by": kwargs["generated_by"],
            "linked_action_id": kwargs["linked_action_id"],
            "status": "pending",
        }

    monkeypatch.setattr(
        "bifrost_research.api.agents.draft_repo.insert_draft", _fake_insert_draft
    )

    app = create_app()
    with TestClient(app) as c:
        c.captured = captured  # type: ignore[attr-defined]
        yield c


def test_create_draft_happy_path_hypothesis(client: TestClient) -> None:
    resp = client.post(
        "/research/drafts",
        json={
            "kind": "hypothesis_suggestion",
            "title": "NVDA earnings vol crush",
            "summary": "IV_rank>=95 pre-earnings, size 0.5R.",
            "hypothesis_id": "hyp_123",
            "symbols": ["nvda", "amd"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    draft = body["data"]["draft"]
    assert draft["kind"] == "hypothesis_suggestion"
    assert draft["scope"] == "hypothesis:hyp_123"
    assert draft["generated_by"] == "owner_manual"

    captured = client.captured  # type: ignore[attr-defined]
    payload = captured["payload"]
    assert payload["title"] == "NVDA earnings vol crush"
    assert payload["manual"] is True
    assert payload["symbols"] == ["NVDA", "AMD"]
    assert payload["hypothesis_id"] == "hyp_123"


def test_create_draft_morning_brief_no_hyp(client: TestClient) -> None:
    resp = client.post(
        "/research/drafts",
        json={
            "kind": "morning_brief",
            "title": "Sept 1 open plan",
            "summary": "Focus IVR>=90 names; ES ranges 4520-4540.",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    draft = body["data"]["draft"]
    assert draft["kind"] == "morning_brief"
    assert draft["scope"] == "manual:morning_brief"

    captured = client.captured  # type: ignore[attr-defined]
    payload = captured["payload"]
    assert payload["create_hypothesis"] is False


def test_reject_invalid_kind_candidate_batch(client: TestClient) -> None:
    resp = client.post(
        "/research/drafts",
        json={
            "kind": "candidate_batch",
            "title": "t",
            "summary": "s",
        },
    )
    assert resp.status_code == 400
    assert "not allowed for manual creation" in resp.text


def test_reject_missing_title(client: TestClient) -> None:
    resp = client.post(
        "/research/drafts",
        json={
            "kind": "hypothesis_suggestion",
            "title": "  ",
            "summary": "some summary",
        },
    )
    assert resp.status_code == 400
    assert "title is required" in resp.text


def test_reject_unknown_kind(client: TestClient) -> None:
    resp = client.post(
        "/research/drafts",
        json={
            "kind": "not_a_real_kind",
            "title": "t",
            "summary": "s",
        },
    )
    assert resp.status_code == 400
