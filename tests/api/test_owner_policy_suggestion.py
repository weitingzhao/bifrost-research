"""The Owner could not change the trading system.

policy_suggestion is not a manually creatable draft kind and PATCH /objectives
only takes status, so the policy was readable and not adjustable — the model
could propose a change and the Owner could not. This endpoint closes that, and
routes the change through a draft so that Owner-proposed and model-proposed
changes leave the same record. Without one, `sepa −3,431 → −1,204` means the
market moved *or* that someone lowered min_score, and those are opposite
conclusions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard", lambda: None
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


OBJECTIVE = {
    "id": "obj-daily-loop-stock",
    "title": "Daily Loop Stock Explorer",
    "policy_json": {
        "universe_mode": "stock_composite",
        "max_candidates": 8,
        "layers": {"sepa": {"required": True, "min_score": 70.0}},
    },
}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("bifrost_research.api.harness.connect", lambda: MagicMock())
    monkeypatch.setattr(
        "bifrost_research.api.harness.obj_repo.get_objective",
        lambda conn, oid: OBJECTIVE if oid == OBJECTIVE["id"] else None,
    )
    monkeypatch.setattr(
        "bifrost_research.api.harness.draft_repo.insert_draft",
        lambda conn, **kw: (seen.update(kw), {"id": "drf-test", **kw})[1],
    )
    return seen


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _post(client: TestClient, body: dict[str, Any], oid: str = OBJECTIVE["id"]) -> Any:
    return client.post(f"/research/objectives/{oid}/policy-suggestion", json=body)


def test_a_change_becomes_a_draft_carrying_its_reason(
    client: TestClient, captured: dict[str, Any]
) -> None:
    r = _post(client, {"suggestion": {"max_candidates": 12}, "rationale": "8 is too few"})
    assert r.status_code == 200, r.text
    assert captured["kind"] == "policy_suggestion"
    payload = captured["payload"]
    assert payload["objective_id"] == OBJECTIVE["id"]
    assert payload["suggestion"] == {"max_candidates": 12}
    assert payload["rationale"] == "8 is too few"
    # Attribution matters: drift is only readable if a change can be traced to
    # whoever made it.
    assert payload["manual"] is True
    assert captured["generated_by"].startswith("owner:")
    # The Inbox diff renders current-vs-proposed from this snapshot. Without it
    # every row showed "not set" as the current value, so an 8 → 10 change read
    # as though nothing had been set before.
    assert payload["current_policy"]["max_candidates"] == 8


def test_a_field_that_would_be_dropped_is_refused(
    client: TestClient, captured: dict[str, Any]
) -> None:
    # patch_policy_json filters silently at approval. Accepting the field here
    # would put a card in the Inbox that changes nothing when approved — the
    # exact failure the "0 fields to merge" work exists to surface.
    r = _post(client, {"suggestion": {"use_llm_plan": False}})
    assert r.status_code == 400
    assert "use_llm_plan" in r.text
    assert "silently" in r.text
    assert captured == {}, "a refused suggestion must not create a draft"


def test_it_names_what_can_be_changed(client: TestClient, captured: dict[str, Any]) -> None:
    r = _post(client, {"suggestion": {"nonsense_knob": 1}})
    assert r.status_code == 400
    assert "max_candidates" in r.text, "the error should say what IS allowed"


def test_a_value_the_schema_rejects_is_refused(
    client: TestClient, captured: dict[str, Any]
) -> None:
    # parse_policy is fail-soft by design — it logs and falls back to defaults
    # so a stored policy can always be read. Validating with it would accept
    # `max_candidates: 9999`, store a policy that says 9999, and have every run
    # quietly do something else.
    r = _post(client, {"suggestion": {"max_candidates": 9999}})
    assert r.status_code == 400
    assert "would not be valid" in r.text
    assert captured == {}


def test_an_empty_suggestion_is_refused(client: TestClient, captured: dict[str, Any]) -> None:
    assert _post(client, {"suggestion": {}}).status_code == 400
    assert captured == {}


def test_an_unknown_objective_is_a_404(client: TestClient, captured: dict[str, Any]) -> None:
    r = _post(client, {"suggestion": {"max_candidates": 12}}, oid="obj-nope")
    assert r.status_code == 404
    assert captured == {}


def test_the_rationale_is_optional_but_kept_clean(
    client: TestClient, captured: dict[str, Any]
) -> None:
    _post(client, {"suggestion": {"max_candidates": 12}, "rationale": "  spaced  "})
    assert captured["payload"]["rationale"] == "spaced"


def test_nested_policy_groups_are_allowed(client: TestClient, captured: dict[str, Any]) -> None:
    # layers is where the real trading style lives; refusing it would leave the
    # only editable knobs the shallow ones.
    r = _post(
        client,
        {"suggestion": {"layers": {"sepa": {"required": True, "min_score": 65.0}}}},
    )
    assert r.status_code == 200, r.text
    assert captured["payload"]["suggestion"]["layers"]["sepa"]["min_score"] == 65.0
