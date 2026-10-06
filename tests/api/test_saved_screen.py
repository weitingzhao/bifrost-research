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
        # The Pine library: one built-in, one of mine switched off.
        self.pine_ids = ["supertrend", "my_old_cross"]


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
        elif q.startswith("SELECT ID FROM RESEARCH.PINE_SCRIPT"):
            self._result = [(i,) for i in self.store.pine_ids]
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


# ── stock_screen.v2 (0.181.0) ────────────────────────────────────────────

V2 = {
    "stages": {
        "agree": {"on": ["m_sepa", "m_radar"], "min": 2},
        "trend": {"on": ["price_gt_sma200"], "min": 0},
        "momtier": {"on": [], "min": 6},
        "radar": {"on": ["grade_a", "grade_b"]},
        "options": {"on": ["ivr_ge_40"]},
    },
    "pine": {"on": ["pine:supertrend:buy"], "window": 10, "match": "all"},
    "universe": "options",
}


def _v2(client: TestClient, definition: Any, name: str = "Pine leaders") -> Any:
    return client.post(
        "/research/screens", json={"name": name, "definition": definition, "vocabulary": repo.VOCABULARY_V2}
    )


def test_v2_roundtrip_keeps_stages_pine_block_and_universe(client: TestClient) -> None:
    r = _v2(client, V2)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["vocabulary"] == "stock_screen.v2"
    assert d["definition"]["stages"]["agree"] == {"on": ["m_sepa", "m_radar"], "min": 2}
    assert d["definition"]["stages"]["momtier"] == {"on": [], "min": 6}
    assert d["definition"]["pine"] == {"on": ["pine:supertrend:buy"], "window": 10, "match": "all"}
    assert d["definition"]["universe"] == "options"
    # read back as stored
    got = client.get(f"/research/screens/{d['id']}").json()["data"]
    assert got["definition"] == d["definition"]


def test_v2_defaults_and_drops_empty_stages(client: TestClient) -> None:
    d = _v2(client, {"stages": {"trend": {"on": []}}}).json()["data"]["definition"]
    assert d == {"stages": {}, "pine": {"on": [], "window": 5, "match": "any"}, "universe": None}


@pytest.mark.parametrize(
    ("bad", "says"),
    [
        ({"stages": {"trend": {"on": ["made_up"]}}}, "made_up"),
        ({"stages": {"trend": {"on": ["grade_a"]}}}, "grade_a"),  # a condition in the wrong stage
        ({"stages": {"quality": {"on": ["fcf_positive"]}}}, "quality"),  # nothing evaluates it
        ({"stages": {"catalyst": {"on": ["earn_lt_10d"]}}}, "earn_lt_10d"),
        ({"stages": {"trend": {"on": [], "min": 12}}}, "stages.trend.min"),
        ({"stages": {"agree": {"on": ["m_sepa"], "min": 2}}}, "stages.agree.min"),
        ({"stages": {"radar": {"on": ["grade_a"], "min": 1}}}, "no 'at least N'"),
        ({"pine": {"on": ["supertrend:buy"]}}, "malformed"),
        ({"pine": {"on": ["pine:never_existed:buy"]}}, "never_existed"),
        ({"pine": {"window": 3}}, "pine.window"),
        ({"pine": {"match": "most"}}, "pine.match"),
        ({"universe": "sp500"}, "sp500"),
        ({"extra": 1}, "extra"),
    ],
)
def test_v2_rejects_drift_by_name(client: TestClient, bad: dict[str, Any], says: str) -> None:
    r = _v2(client, bad)
    assert r.status_code == 422
    assert says in r.json()["detail"]


def test_v2_keeps_a_switched_off_script(client: TestClient) -> None:
    r = _v2(client, {"pine": {"on": ["pine:my_old_cross:sell"]}})
    assert r.status_code == 200
    assert r.json()["data"]["definition"]["pine"]["on"] == ["pine:my_old_cross:sell"]


def test_unknown_vocabulary_is_422(client: TestClient) -> None:
    r = client.post("/research/screens", json={"name": "X", "definition": V2, "vocabulary": "stock_screen.v9"})
    assert r.status_code == 422
    assert "stock_screen.v9" in r.json()["detail"]


def test_patch_validates_against_the_rows_own_stamp_and_moves_stamps_with_a_definition(client: TestClient) -> None:
    v2 = _v2(client, V2).json()["data"]
    # a v2 row's definition is checked as v2 without restating the stamp
    r = client.patch(f"/research/screens/{v2['id']}", json={"definition": dict(V2, universe="watch")})
    assert r.status_code == 200
    assert r.json()["data"]["definition"]["universe"] == "watch"
    # a v1 definition sent to a v2 row is drift
    assert client.patch(f"/research/screens/{v2['id']}", json={"definition": DEFN}).status_code == 422

    v1 = client.post("/research/screens", json={"name": "Old", "definition": DEFN}).json()["data"]
    # a stamp alone is refused: the old definition does not speak it
    r = client.patch(f"/research/screens/{v1['id']}", json={"vocabulary": repo.VOCABULARY_V2})
    assert r.status_code == 422
    r = client.patch(
        f"/research/screens/{v1['id']}", json={"vocabulary": repo.VOCABULARY_V2, "definition": V2}
    )
    assert r.status_code == 200
    assert r.json()["data"]["vocabulary"] == repo.VOCABULARY_V2


def test_vocabulary_route_is_not_an_id_and_lists_both_stamps(client: TestClient) -> None:
    r = client.get("/research/screens/vocabulary")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["versions"] == ["sepa_screener_wide.v1", "stock_screen.v2"]
    v2 = d["stock_screen.v2"]
    assert v2["pine"] == {"windows": [1, 5, 10], "match": ["any", "all"], "scripts": ["my_old_cross", "supertrend"]}
    assert v2["universes"] == ["all", "options", "watch", "book"]
    assert v2["stages"]["momtier"]["max"] == 10


def test_v2_catalog_is_pinned() -> None:
    # Stock screen's live conditions (frontend stockScreenStages.ts); the
    # frontend asserts its chips are a subset of /research/screens/vocabulary.
    counts = {sid: len(c["conditions"]) for sid, c in repo.STAGES_V2.items()}
    assert counts == {
        "agree": 3,
        "trend": 11,
        "growth": 8,
        "momtier": 10,
        "radar": 5,
        "structure": 8,
        "sentiment": 6,
        "catalyst": 4,
        "options": 3,
    }
    assert {c["kind"] for c in repo.STAGES_V2.values()} == {"agree", "min", "any", "all"}

