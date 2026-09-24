"""Saved-screen CRUD API tests — 6A.

In-memory fake Postgres, same approach as test_hypothesis.py: just enough
SQL dispatch to serve the repository. All fixture values invented.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api import saved_screen as api
from bifrost_research.api.app import create_app
from bifrost_research.repositories import saved_screen as repo


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}


class _FakeCursor:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def _tuple(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[c] for c in ("id", "name", "description", "definition", "vocabulary", "is_active", "origin_page", "created_at", "updated_at", "retired_at"))

    def execute(self, query: str, params: Any = None) -> None:
        params = tuple(params) if params else ()
        q = " ".join(query.split()).strip().upper()
        now = datetime(2026, 9, 24, tzinfo=timezone.utc)
        if q.startswith("INSERT INTO RESEARCH.SAVED_SCREEN"):
            screen_id, name, description, definition, vocabulary, origin_page = params
            row = {
                "id": screen_id,
                "name": name,
                "description": description,
                "definition": definition,
                "vocabulary": vocabulary,
                "is_active": True,
                "origin_page": origin_page,
                "created_at": now,
                "updated_at": now,
                "retired_at": None,
            }
            self.store.rows[screen_id] = row
            self._result = [self._tuple(row)]
        elif q.startswith("UPDATE RESEARCH.SAVED_SCREEN SET RETIRED_AT"):
            (screen_id,) = params
            row = self.store.rows.get(screen_id)
            if row is None or row["retired_at"] is not None:
                self._result = []
            else:
                row["retired_at"] = now
                row["is_active"] = False
                row["updated_at"] = now
                self._result = [self._tuple(row)]
        elif q.startswith("UPDATE RESEARCH.SAVED_SCREEN SET"):
            *values, screen_id = params
            row = self.store.rows.get(screen_id)
            if row is None:
                self._result = []
                return
            cols = [frag.split("=")[0].strip().lower() for frag in q[q.index("SET") + 3 : q.index("WHERE")].split(",")]
            cols = [c for c in cols if c != "updated_at"]
            for col, val in zip(cols, values):
                row[col] = val
            row["updated_at"] = now
            self._result = [self._tuple(row)]
        elif q.startswith("SELECT") and "WHERE ID = " in q:
            (screen_id,) = params
            row = self.store.rows.get(screen_id)
            self._result = [self._tuple(row)] if row else []
        elif q.startswith("SELECT"):
            rows = list(self.store.rows.values())
            if "RETIRED_AT IS NULL" in q:
                rows = [r for r in rows if r["retired_at"] is None]
            self._result = [self._tuple(r) for r in rows]
        else:  # pragma: no cover
            raise AssertionError(f"unhandled query: {q[:80]}")

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    def close(self) -> None:
        return None


class _FakeConn:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.store)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    store = _FakeStore()
    monkeypatch.setattr(api, "connect", lambda: _FakeConn(store))
    return TestClient(create_app())


DEFN = {
    "q": "",
    "paths": ["PIVOT"],
    "grades": ["A+", "A"],
    "min_composite": 60,
    "tech": ["crs_ge_70", "price_gt_sma50"],
    "fund": ["eps_3y_ge_15pct"],
}


def test_create_list_get_roundtrip(client: TestClient) -> None:
    r = client.post("/research/screens", json={"name": "Leaders over SMA50", "definition": DEFN})
    assert r.status_code == 200
    created = r.json()["data"]
    assert created["id"].startswith("scr-leaders-over-sma50-")
    assert created["vocabulary"] == repo.VOCABULARY_V1
    assert created["definition"]["paths"] == ["PIVOT"]

    r = client.get("/research/screens")
    assert r.status_code == 200
    assert r.json()["data"]["count"] == 1

    r = client.get(f"/research/screens/{created['id']}")
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "Leaders over SMA50"


def test_unknown_condition_is_422_not_a_passthrough(client: TestClient) -> None:
    bad = dict(DEFN, tech=["crs_ge_70", "made_up_condition"])
    r = client.post("/research/screens", json={"name": "Bad", "definition": bad})
    assert r.status_code == 422
    assert "made_up_condition" in r.json()["detail"]
    # unknown top-level keys are drift, not extras
    r = client.post("/research/screens", json={"name": "Bad2", "definition": dict(DEFN, extra=1)})
    assert r.status_code == 422


def test_patch_revalidates_definition_and_retire_soft_deletes(client: TestClient) -> None:
    created = client.post("/research/screens", json={"name": "S", "definition": DEFN}).json()["data"]
    sid = created["id"]

    r = client.patch(f"/research/screens/{sid}", json={"definition": dict(DEFN, min_composite=150)})
    assert r.status_code == 422

    r = client.patch(f"/research/screens/{sid}", json={"name": "S2", "definition": dict(DEFN, min_composite=70)})
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "S2"
    assert r.json()["data"]["definition"]["min_composite"] == 70

    r = client.post(f"/research/screens/{sid}/retire")
    assert r.status_code == 200
    assert r.json()["data"]["retired_at"] is not None

    assert client.get("/research/screens").json()["data"]["count"] == 0
    assert client.get("/research/screens?include_retired=true").json()["data"]["count"] == 1
    # retire twice → 404, not a silent ok
    assert client.post(f"/research/screens/{sid}/retire").status_code == 404


def test_definition_catalog_matches_the_mart() -> None:
    # The catalogs must stay 11 + 8 — the wide table's own condition columns.
    assert len(repo.TECH_CONDITIONS_V1) == 11
    assert len(repo.FUND_CONDITIONS_V1) == 8
