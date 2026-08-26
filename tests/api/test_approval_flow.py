"""Wave RS-E4.2 — approval token issue / validate / replay / expiry / mismatch."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.copilot.approvals import (
    ApprovalError,
    issue_token,
    reset_consumed_for_tests,
    validate_token,
)
from bifrost_research.copilot.rate_limit import reset_usage_for_tests


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_consumed_for_tests()
    reset_usage_for_tests()
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard",
        lambda: None,
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None
    yield
    reset_consumed_for_tests()
    reset_usage_for_tests()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Avoid real DB for approve/execute audit paths
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
    fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(
        "bifrost_research.api.copilot.connect",
        lambda: fake_conn,
    )
    monkeypatch.setattr(
        "bifrost_research.api.copilot.action_repo.insert_action",
        lambda *a, **k: {"id": k.get("action_id") or "aal_x", "status": "approved"},
    )
    monkeypatch.setattr(
        "bifrost_research.api.copilot.action_repo.get_action",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "bifrost_research.api.copilot.action_repo.update_action_status",
        lambda *a, **k: {"id": a[1] if len(a) > 1 else "aal_x", "status": k.get("status")},
    )
    monkeypatch.setattr(
        "bifrost_research.api.copilot.action_repo.generate_action_id",
        lambda: "aal_fixed_test",
    )

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_issue_and_validate_ok() -> None:
    args = {"title": "t", "thesis": "x"}
    issued = issue_token(action_id="aal_1", tool="research.hypothesis.create", arguments=args)
    meta = validate_token(
        issued["approval_token"],
        tool="research.hypothesis.create",
        arguments=args,
    )
    assert meta["action_id"] == "aal_1"


def test_replay_raises_409() -> None:
    args = {"title": "t", "thesis": "x"}
    issued = issue_token(action_id="aal_2", tool="research.hypothesis.create", arguments=args)
    validate_token(
        issued["approval_token"],
        tool="research.hypothesis.create",
        arguments=args,
    )
    with pytest.raises(ApprovalError) as ei:
        validate_token(
            issued["approval_token"],
            tool="research.hypothesis.create",
            arguments=args,
        )
    assert ei.value.status == 409


def test_expired_raises_410(monkeypatch: pytest.MonkeyPatch) -> None:
    args = {"hypothesis_id": "h1", "status": "validated"}
    clock = {"t": 1_700_000_000.0}

    def _now() -> float:
        return clock["t"]

    monkeypatch.setattr(
        "bifrost_research.copilot.approvals.time.time",
        _now,
    )
    issued = issue_token(
        action_id="aal_3",
        tool="research.hypothesis.patch",
        arguments=args,
        ttl_sec=60,
    )
    clock["t"] += 61
    with pytest.raises(ApprovalError) as ei:
        validate_token(
            issued["approval_token"],
            tool="research.hypothesis.patch",
            arguments=args,
        )
    assert ei.value.status == 410


def test_hash_mismatch_raises_400() -> None:
    args = {"title": "t", "thesis": "x"}
    issued = issue_token(action_id="aal_4", tool="research.hypothesis.create", arguments=args)
    with pytest.raises(ApprovalError) as ei:
        validate_token(
            issued["approval_token"],
            tool="research.hypothesis.create",
            arguments={"title": "TAMPERED", "thesis": "x"},
        )
    assert ei.value.status == 400
    assert "hash" in ei.value.message.lower()


def test_approve_endpoint_returns_token(client: TestClient) -> None:
    res = client.post(
        "/research/copilot/approve",
        json={
            "tool_name": "research.hypothesis.create",
            "arguments": {"title": "NVDA crush", "thesis": "IV crush"},
            "session_id": "sess-1",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "approval_token" in body["data"]
    assert body["data"]["action_id"] == "aal_fixed_test"
    assert body["data"]["expires_in_sec"] == 60


def test_approve_rejects_read_tool(client: TestClient) -> None:
    res = client.post(
        "/research/copilot/approve",
        json={
            "tool_name": "research.hypothesis.list",
            "arguments": {},
        },
    )
    assert res.status_code == 400


def test_execute_http_codes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    args = {
        "title": "NVDA",
        "thesis": "crush",
        "symbols": [],
        "tags": [],
        "status": "active",
        "origin_page": "copilot",
        "conclusion": None,
    }
    issued = issue_token(
        action_id="aal_fixed_test",
        tool="research.hypothesis.create",
        arguments=args,
    )

    async def _fake_exec(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "data": {"diff_kind": "create_hypothesis", "executed": True, "result": {"id": "h1"}},
        }

    monkeypatch.setattr(
        "bifrost_research.api.copilot.execute_approved_write",
        _fake_exec,
    )
    res = client.post(
        "/research/copilot/execute",
        json={
            "approval_token": issued["approval_token"],
            "tool_name": "research.hypothesis.create",
            "arguments": args,
            "action_id": "aal_fixed_test",
        },
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # missing token
    res2 = client.post(
        "/research/copilot/execute",
        json={
            "approval_token": "",
            "tool_name": "research.hypothesis.create",
            "arguments": args,
        },
    )
    assert res2.status_code == 403


def test_execute_replay_via_tool_path() -> None:
    """Second validate on same token → 409 (unit)."""
    args = {"hypothesis_id": "h1"}
    issued = issue_token(
        action_id="aal_5",
        tool="research.hypothesis.retire",
        arguments=args,
    )
    validate_token(
        issued["approval_token"],
        tool="research.hypothesis.retire",
        arguments=args,
    )
    with pytest.raises(ApprovalError) as ei:
        validate_token(
            issued["approval_token"],
            tool="research.hypothesis.retire",
            arguments=args,
        )
    assert ei.value.status == 409
