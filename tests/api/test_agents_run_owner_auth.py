"""Running an agent by hand needs a research user.

Each run writes drafts and action rows (the digest also calls a model), so an
anonymous caller must not start one. The console sends both of its run buttons
(morning, EOD) through the drafts helper, which carries the research bearer;
the schedules call the Python directly, not these routes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map

# route -> (module, function) the route imports at call time
_RUNNERS = {
    "/research/agents/morning/run": (
        "bifrost_research.copilot.agents.morning_prep",
        "run_morning_prep",
    ),
    "/research/agents/digest/run": (
        "bifrost_research.copilot.agents.daily_digest",
        "run_daily_digest",
    ),
    "/research/agents/weekly-policy/run": (
        "bifrost_research.copilot.agents.weekly_policy_review",
        "run_weekly_policy_review",
    ),
    "/research/agents/eod/run": (
        "bifrost_research.copilot.agents.eod_review",
        "run_eod_review",
    ),
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
    monkeypatch.delenv("BIFROST_MORNING_AGENT_DRY_RUN", raising=False)
    monkeypatch.delenv("BIFROST_EOD_AGENT_DRY_RUN", raising=False)
    token_to_owner_map.cache_clear()
    yield
    token_to_owner_map.cache_clear()


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every agent runner; record which ran. The real ones open the DB."""
    ran: list[str] = []
    for path, (module, fn) in _RUNNERS.items():

        def _fake(*_a: Any, _path: str = path, **_k: Any) -> dict[str, Any]:
            ran.append(_path)
            return {"agent": _path, "count": 0}

        monkeypatch.setattr(f"{module}.{fn}", _fake)
    return ran


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize("path", list(_RUNNERS))
def test_anonymous_run_is_refused(
    client: TestClient, runs: list[str], auth_on: None, path: str
) -> None:
    resp = client.post(path)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert runs == []


@pytest.mark.parametrize("path", list(_RUNNERS))
def test_unknown_token_is_refused(
    client: TestClient, runs: list[str], auth_on: None, path: str
) -> None:
    resp = client.post(path, headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert runs == []


@pytest.mark.parametrize("path", list(_RUNNERS))
def test_research_user_can_run(
    client: TestClient, runs: list[str], auth_on: None, path: str
) -> None:
    resp = client.post(path, headers={"Authorization": "Bearer tok_alice"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["agent"] == path
    assert runs == [path]
