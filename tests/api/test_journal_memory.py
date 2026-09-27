"""Journal memory API (K6) — router rules with the repo patched out."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app

REPO = "bifrost_research.repositories.journal_memory"


@pytest.fixture(autouse=True)
def _patch_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard",
        lambda: None,
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_memory_index_hides_archived_but_axes_read_them(client: TestClient) -> None:
    rows = [
        {"id": "M-1", "topic": "axis-hold", "axis": "hold", "archived": False, "strength": 0.8,
         "value": "9d", "sub": "median", "text": "", "change": "steady", "kind": "did"},
        {"id": "M-2", "topic": "axis-exit", "axis": "exit", "archived": True, "strength": 0.2,
         "value": "Half the credit", "sub": "", "text": "", "change": "fading", "kind": "did"},
    ]
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(f"{REPO}.list_memories", return_value=rows),
        patch(f"{REPO}.list_sources", return_value=[{"source": "fills", "enabled": True}]),
        patch(f"{REPO}.hint_counts", return_value={}),
        patch(f"{REPO}.week_summary", return_value={"range": "x", "moved": 0}),
    ):
        res = client.get("/research/journal/memory")
    data = res.json()["data"]
    assert [m["id"] for m in data["memories"]] == ["M-1"]
    assert data["archived_count"] == 1
    # §20.3 — the archived exit memory still backs the portrait axis
    assert {a["id"] for a in data["axes"]} == {"hold", "exit"}


def test_memory_forget_answers_404_for_a_stranger(client: TestClient) -> None:
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(f"{REPO}.forget_memory", return_value=None),
    ):
        assert client.delete("/research/journal/memory/M-99").status_code == 404
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(f"{REPO}.forget_memory", return_value="axis-hold"),
    ):
        res = client.delete("/research/journal/memory/M-1")
    assert res.status_code == 200 and res.json()["data"]["topic"] == "axis-hold"


def test_source_set_rejects_unknown_source(client: TestClient) -> None:
    with patch("bifrost_research.api.journal.connect", return_value=MagicMock()):
        res = client.put("/research/journal/memory/sources/palantir", json={"enabled": False})
    assert res.status_code == 400


def test_visit_requires_a_route_path(client: TestClient) -> None:
    with patch("bifrost_research.api.journal.connect", return_value=MagicMock()):
        assert (
            client.post("/research/journal/visits", json={"route": "nope"}).status_code == 400
        )
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(f"{REPO}.insert_visit") as ins,
    ):
        res = client.post(
            "/research/journal/visits", json={"route": "/research/symbol", "symbol": "zztm"}
        )
    assert res.status_code == 200
    assert ins.call_args.kwargs["symbol"] == "zztm"[:12]


def test_day_rejects_a_malformed_date(client: TestClient) -> None:
    with patch("bifrost_research.api.journal.connect", return_value=MagicMock()):
        assert client.get("/research/journal/day", params={"date": "nope"}).status_code == 400


def test_hint_dismiss_reports_quiet_at_the_threshold(client: TestClient) -> None:
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(f"{REPO}.dismiss_hint", return_value=3),
    ):
        res = client.post("/research/journal/memory/hint/axis-trigger/dismiss")
    data = res.json()["data"]
    assert data["count"] == 3 and data["quiet"] is True
