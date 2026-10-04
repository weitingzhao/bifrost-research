"""Journal notes API (K4) — router rules with the repo patched out.

The §20.1 lock, ref normalization and the owner scope are the contract; the
SQL beneath them is exercised against the real store on DEV.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.repositories.journal_notes import (
    REF_TYPES,
    NoteLockedError,
    TradeRefEnvError,
    normalize_refs,
    present_refs,
)


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
            {"type": "trade", "id": ""},  # blank id falls out
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


# ── TD-73 / naming R0 — trade refs are stored per environment ───────────────

ENV = {"X-Bifrost-Env": "prod"}


def test_trade_ref_is_stored_env_qualified_under_its_new_code() -> None:
    refs = normalize_refs(
        [
            {"type": "inst", "id": "158"},  # the old code: not a type since naming R4
            {"type": "trade", "id": "#158"},
            {"type": "trade", "id": "prod:159"},  # already this env's
            {"type": "trade", "id": "not-a-number"},  # falls out like a blank id
        ],
        "prod",
    )
    assert refs == [
        {"type": "trade", "id": "prod:158"},
        {"type": "trade", "id": "prod:159"},
    ]


def test_the_old_inst_code_is_no_ref_type() -> None:
    # naming R4 (0.162.0): `inst` was accepted one release and stored as `trade`; 0 stored.
    assert "inst" not in REF_TYPES
    assert normalize_refs([{"type": "inst", "id": "158"}], "prod") == []


def test_trade_ref_without_env_or_from_another_env_raises() -> None:
    with pytest.raises(TradeRefEnvError):
        normalize_refs([{"type": "trade", "id": "158"}])
    with pytest.raises(TradeRefEnvError):
        normalize_refs([{"type": "trade", "id": "dev:158"}], "prod")
    # Notes that link no trade need no environment (old clients keep working).
    assert normalize_refs([{"type": "sym", "id": "zztm"}]) == [{"type": "sym", "id": "ZZTM"}]


def test_present_refs_strips_only_this_envs_prefix() -> None:
    stored = [
        {"type": "trade", "id": "prod:158"},
        {"type": "trade", "id": "dev:158"},
        {"type": "sym", "id": "ZZTM"},
    ]
    assert present_refs(stored, "prod") == [
        {"type": "trade", "id": "158"},
        {"type": "trade", "id": "dev:158"},
        {"type": "sym", "id": "ZZTM"},
    ]
    assert present_refs(stored, None) == stored


def test_create_with_trade_ref_and_env_header_qualifies(client: TestClient) -> None:
    created = {"id": "n1", "owner_id": "owner", "body_md": "hello", "refs": []}
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(
            "bifrost_research.repositories.journal_notes.insert_note", return_value=created
        ) as ins,
    ):
        res = client.post(
            "/research/journal/notes",
            headers=ENV,
            json={"body_md": "hello", "refs": [{"type": "trade", "id": "158"}]},
        )
    assert res.status_code == 200
    assert ins.call_args.kwargs["refs"] == [{"type": "trade", "id": "prod:158"}]
    assert ins.call_args.kwargs["env"] == "prod"


def test_trade_ref_without_env_header_is_400_and_writes_nothing(client: TestClient) -> None:
    """An old client (or a caller that bypassed the Trade gateway) cannot say
    which environment's #158 it means — refused, never guessed."""
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()) as conn,
        patch("bifrost_research.repositories.journal_notes.insert_note") as ins,
    ):
        res = client.post(
            "/research/journal/notes",
            json={"body_md": "hello", "refs": [{"type": "trade", "id": "158"}]},
        )
        patched = client.patch(
            "/research/journal/notes/n1", json={"refs": [{"type": "trade", "id": "158"}]}
        )
    assert res.status_code == 400
    assert "X-Bifrost-Env" in res.json()["detail"]
    assert patched.status_code == 400
    ins.assert_not_called()
    conn.assert_not_called()


def test_symbol_note_without_env_header_still_saves(client: TestClient) -> None:
    created = {"id": "n2", "owner_id": "owner", "body_md": "x", "refs": []}
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(
            "bifrost_research.repositories.journal_notes.insert_note", return_value=created
        ) as ins,
    ):
        res = client.post(
            "/research/journal/notes", json={"body_md": "x", "refs": [{"type": "sym", "id": "q"}]}
        )
    assert res.status_code == 200
    assert ins.call_args.kwargs["env"] is None


def test_unknown_env_header_is_400(client: TestClient) -> None:
    with patch("bifrost_research.api.journal.connect", return_value=MagicMock()):
        res = client.get("/research/journal/notes", headers={"X-Bifrost-Env": "qa"})
    assert res.status_code == 400


def test_trade_filter_takes_the_env_and_the_new_code(client: TestClient) -> None:
    with (
        patch("bifrost_research.api.journal.connect", return_value=MagicMock()),
        patch(
            "bifrost_research.repositories.journal_notes.list_notes", return_value=[]
        ) as ls,
    ):
        own = client.get(
            "/research/journal/notes", headers=ENV, params={"ref_type": "trade", "ref_id": "158"}
        )
        other = client.get(
            "/research/journal/notes",
            headers=ENV,
            params={"ref_type": "trade", "ref_id": "dev:158"},
        )
        bare_no_env = client.get(
            "/research/journal/notes", params={"ref_type": "trade", "ref_id": "158"}
        )
        old_code = client.get(
            "/research/journal/notes", headers=ENV, params={"ref_type": "inst", "ref_id": "158"}
        )
    assert own.status_code == 200
    assert other.status_code == 200
    assert bare_no_env.status_code == 400
    assert old_code.status_code == 400  # naming R4: `inst` is not a ref type
    first, second = (c.kwargs for c in ls.call_args_list)
    assert (first["ref_type"], first["ref_id"], first["env"]) == ("trade", "prod:158", "prod")
    assert (second["ref_type"], second["ref_id"]) == ("trade", "dev:158")
