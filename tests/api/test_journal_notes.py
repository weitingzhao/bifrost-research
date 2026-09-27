"""Journal notes API (K4) — router rules with the repo patched out.

The §20.1 lock, ref normalization and the owner scope are the contract; the
SQL beneath them is exercised against the real store on DEV.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.repositories.journal_notes import NoteLockedError, normalize_refs


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


def test_normalize_refs_keeps_the_designs_shape() -> None:
    refs = normalize_refs(
        [
            {"type": "sym", "id": "zztm"},
            {"type": "sym", "id": "ZZTM"},  # duplicate after uppercasing
            {"type": "obj", "id": "obj-daily-stock"},
            {"type": "inst", "id": ""},  # blank id falls out
            {"type": "nope", "id": "x"},  # unknown type falls out
            "not-a-dict",
        ]
    )
    assert refs == [
        {"type": "sym", "id": "ZZTM"},
        {"type": "obj", "id": "obj-daily-stock"},
    ]


def test_create_note_normalizes_and_returns(client: TestClient) -> None:
    created = {"id": "n1", "owner_id": "owner", "body_md": "hello", "refs": []}
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(
            "bifrost_research.repositories.journal_notes.insert_note", return_value=created
        ) as ins,
    ):
        res = client.post(
            "/research/journal/notes",
            json={"body_md": "  hello  ", "refs": [{"type": "sym", "id": "zztm"}]},
        )
    assert res.status_code == 200
    assert res.json()["data"]["note"]["id"] == "n1"
    kwargs = ins.call_args.kwargs
    assert kwargs["body_md"] == "hello"
    assert kwargs["refs"] == [{"type": "sym", "id": "ZZTM"}]


def test_create_note_requires_body(client: TestClient) -> None:
    with patch("bifrost_research.api.journal.connect", return_value=MagicMock()):
        res = client.post("/research/journal/notes", json={"body_md": "   "})
    assert res.status_code == 400


def test_list_rejects_unknown_ref_type(client: TestClient) -> None:
    with patch("bifrost_research.api.journal.connect", return_value=MagicMock()):
        res = client.get("/research/journal/notes", params={"ref_type": "page", "ref_id": "x"})
    assert res.status_code == 400


def test_locked_note_answers_409_on_patch_and_delete(client: TestClient) -> None:
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(
            "bifrost_research.repositories.journal_notes.update_note",
            side_effect=NoteLockedError("M-31"),
        ),
        patch(
            "bifrost_research.repositories.journal_notes.delete_note",
            side_effect=NoteLockedError("M-31"),
        ),
    ):
        patched = client.patch("/research/journal/notes/n1", json={"body_md": "x"})
        deleted = client.delete("/research/journal/notes/n1")
    assert patched.status_code == 409
    assert "M-31" in patched.json()["detail"]
    assert deleted.status_code == 409


def test_missing_note_is_404(client: TestClient) -> None:
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch("bifrost_research.repositories.journal_notes.update_note", return_value=None),
        patch("bifrost_research.repositories.journal_notes.delete_note", return_value=False),
    ):
        assert client.patch("/research/journal/notes/nx", json={"body_md": "x"}).status_code == 404
        assert client.delete("/research/journal/notes/nx").status_code == 404
