"""Hypothesis CRUD API tests — Wave RS-A.

Uses an in-memory fake Postgres connection so tests run without a live DB.
The fake mimics psycopg2 array/jsonb semantics closely enough for the repo.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api import hypothesis as hypothesis_api
from bifrost_research.api.app import create_app


# ---------------------------------------------------------------------------
# Fake Postgres — just enough to serve the repo's SQL
# ---------------------------------------------------------------------------


class _CountRow:
    """Marker row for GROUP BY status count queries."""

    __slots__ = ("status", "count")

    def __init__(self, *, status: str, count: int) -> None:
        self.status = status
        self.count = count


class _FakeStore:
    """Backing store shared between all connections in a test."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}


class _FakeCursor:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store
        self._last_result: list[dict[str, Any]] = []

    # ---- lifecycle ----
    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def close(self) -> None:
        return None

    # ---- query dispatch ----
    def execute(self, query: str, params: Any = None) -> None:
        params = tuple(params) if params else ()
        q = " ".join(query.split()).strip()
        upper = q.upper()
        if upper.startswith("INSERT INTO RESEARCH.HYPOTHESIS"):
            self._do_insert(params)
        elif upper.startswith("SELECT ID, TITLE, THESIS, SYMBOLS"):
            self._do_select(q, params)
        elif upper.startswith("UPDATE RESEARCH.HYPOTHESIS SET RETIRED_AT"):
            self._do_retire(params)
        elif upper.startswith("UPDATE RESEARCH.HYPOTHESIS"):
            self._do_update(q, params)
        elif upper.startswith("SELECT STATUS, COUNT"):
            self._do_count_by_status()
        else:
            raise NotImplementedError(f"fake cursor cannot handle: {q!r}")

    def fetchone(self) -> Any:
        if not self._last_result:
            return None
        row = self._last_result[0]
        if isinstance(row, _CountRow):
            return (row.status, row.count)
        if isinstance(row, dict):
            return _row_tuple(row)
        return row

    def fetchall(self) -> list[Any]:
        out: list[Any] = []
        for row in self._last_result:
            if isinstance(row, _CountRow):
                out.append((row.status, row.count))
            elif isinstance(row, dict):
                out.append(_row_tuple(row))
            else:
                out.append(row)
        return out

    # ---- handlers ----
    def _do_insert(self, params: tuple[Any, ...]) -> None:
        (
            hid,
            title,
            thesis,
            symbols,
            tags,
            status,
            origin_page,
            origin_ref,
            linked_opportunity_ids,
            linked_backtest_ids,
            conclusion,
        ) = params
        now = datetime.now(timezone.utc)
        row = {
            "id": hid,
            "title": title,
            "thesis": thesis,
            "symbols": list(symbols or []),
            "tags": list(tags or []),
            "status": status,
            "origin_page": origin_page,
            "origin_ref": _load_json(origin_ref),
            "linked_opportunity_ids": list(linked_opportunity_ids or []),
            "linked_backtest_ids": list(linked_backtest_ids or []),
            "conclusion": conclusion,
            "created_at": now,
            "updated_at": now,
            "retired_at": None,
        }
        self.store.rows[hid] = row
        self._last_result = [deepcopy(row)]

    def _do_select(self, q: str, params: tuple[Any, ...]) -> None:
        clauses = _extract_where_clauses(q)
        has_limit_offset = " LIMIT %s" in q
        params_iter = iter(params)
        # Bind clause parameters once (out of the row loop).
        clause_bindings: list[tuple[str, Any]] = []
        for clause in clauses:
            if clause == "retired_at IS NULL":
                clause_bindings.append((clause, None))
            else:
                try:
                    clause_bindings.append((clause, next(params_iter)))
                except StopIteration as exc:
                    raise AssertionError(
                        f"missing param for clause {clause!r} in query {q!r}"
                    ) from exc
        filtered: list[dict[str, Any]] = []
        for row in self.store.rows.values():
            keep = True
            for clause, value in clause_bindings:
                if clause == "retired_at IS NULL":
                    if row.get("retired_at") is not None:
                        keep = False
                        break
                elif clause == "id = %s":
                    if row["id"] != value:
                        keep = False
                        break
                elif clause == "status = %s":
                    if row["status"] != value:
                        keep = False
                        break
                elif clause == "%s = ANY(symbols)":
                    if value not in row["symbols"]:
                        keep = False
                        break
                elif clause == "%s = ANY(tags)":
                    if value not in row["tags"]:
                        keep = False
                        break
                else:
                    raise NotImplementedError(f"unknown where clause: {clause!r}")
            if keep:
                filtered.append(deepcopy(row))
        filtered.sort(key=lambda r: r["updated_at"], reverse=True)
        if has_limit_offset:
            limit = int(_consume(params_iter, default=len(filtered)))
            offset = int(_consume(params_iter, default=0))
            filtered = filtered[offset : offset + limit]
        self._last_result = filtered

    def _do_update(self, q: str, params: tuple[Any, ...]) -> None:
        assignments = _extract_assignments(q)
        values = list(params)
        hid = values[-1]
        row = self.store.rows.get(hid)
        if row is None:
            self._last_result = []
            return
        for col, val in zip(assignments, values[: len(assignments)]):
            if col == "origin_ref":
                row[col] = _load_json(val)
            else:
                row[col] = list(val) if isinstance(val, (list, tuple)) else val
        row["updated_at"] = datetime.now(timezone.utc)
        self._last_result = [deepcopy(row)]

    def _do_retire(self, params: tuple[Any, ...]) -> None:
        (hid,) = params
        row = self.store.rows.get(hid)
        if row is None or row["retired_at"] is not None:
            self._last_result = []
            return
        now = datetime.now(timezone.utc)
        row["retired_at"] = now
        row["status"] = "archived"
        row["updated_at"] = now
        self._last_result = [deepcopy(row)]

    def _do_count_by_status(self) -> None:
        counts: dict[str, int] = {}
        for row in self.store.rows.values():
            if row.get("retired_at") is not None:
                continue
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        # Return raw (status, count) tuples — this branch is not a hypothesis row.
        self._last_result = [_CountRow(status=s, count=c) for s, c in counts.items()]


class _FakeConnection:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.store)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_COLUMN_ORDER = (
    "id",
    "title",
    "thesis",
    "symbols",
    "tags",
    "status",
    "origin_page",
    "origin_ref",
    "linked_opportunity_ids",
    "linked_backtest_ids",
    "conclusion",
    "created_at",
    "updated_at",
    "retired_at",
)


def _row_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    out: list[Any] = []
    for col in _COLUMN_ORDER:
        val = row.get(col)
        if col == "origin_ref" and val is not None:
            out.append(json.dumps(val))
        else:
            out.append(val)
    return tuple(out)


def _load_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def _extract_where_clauses(q: str) -> list[str]:
    upper = q.upper()
    if " WHERE " not in upper:
        return []
    where_body = q.split("WHERE", 1)[1]
    tail_markers = (" ORDER BY ", " LIMIT ", " RETURNING ")
    lower_body = where_body
    for marker in tail_markers:
        idx = lower_body.upper().find(marker)
        if idx != -1:
            lower_body = lower_body[:idx]
    clauses = [c.strip() for c in lower_body.split(" AND ")]
    return [c for c in clauses if c]


def _extract_assignments(q: str) -> list[str]:
    body = q.split("SET", 1)[1]
    body = body.split("WHERE", 1)[0]
    parts = [p.strip() for p in body.split(",")]
    cols: list[str] = []
    for part in parts:
        if not part:
            continue
        col = part.split("=", 1)[0].strip()
        if col == "updated_at":
            continue
        cols.append(col)
    return cols


def _consume(it: Any, *, default: Any) -> Any:
    try:
        return next(it)
    except StopIteration:
        return default


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, store: _FakeStore) -> TestClient:
    def fake_connect() -> Any:
        return _FakeConnection(store)

    monkeypatch.setattr(hypothesis_api, "connect", fake_connect)
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_hypothesis_routes_registered() -> None:
    client = TestClient(create_app())
    paths = set(client.app.openapi()["paths"])
    assert "/research/hypothesis" in paths
    assert "/research/hypothesis/{hypothesis_id}" in paths
    assert "/research/hypothesis/{hypothesis_id}/retire" in paths
    assert "/research/hypothesis/summary/active" in paths


# ---------------------------------------------------------------------------
# CRUD roundtrip
# ---------------------------------------------------------------------------


def test_create_hypothesis_returns_envelope(client: TestClient) -> None:
    resp = client.post(
        "/research/hypothesis",
        json={
            "title": "NVDA earnings vol crush",
            "thesis": "IV term structure crushes after print",
            "symbols": ["nvda", "amd"],
            "tags": ["earnings"],
            "origin_page": "sepa-daily-core",
            "origin_ref": {"symbol": "NVDA", "date": "2026-08-25"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["title"] == "NVDA earnings vol crush"
    assert data["symbols"] == ["NVDA", "AMD"]
    assert data["origin_ref"] == {"symbol": "NVDA", "date": "2026-08-25"}
    assert data["status"] == "active"
    assert data["id"]


def test_list_and_filter_hypothesis(client: TestClient) -> None:
    for i in range(3):
        client.post(
            "/research/hypothesis",
            json={
                "title": f"H {i}",
                "thesis": "T",
                "symbols": ["NVDA" if i == 0 else "AAPL"],
                "tags": ["earnings"] if i < 2 else ["macro"],
            },
        )
    resp = client.get("/research/hypothesis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["count"] == 3

    resp = client.get("/research/hypothesis", params={"symbol": "NVDA"})
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 1

    resp = client.get("/research/hypothesis", params={"tag": "macro"})
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 1


def test_patch_hypothesis(client: TestClient) -> None:
    created = client.post(
        "/research/hypothesis",
        json={"title": "Init", "thesis": "..."},
    ).json()["data"]
    hid = created["id"]
    resp = client.patch(
        f"/research/hypothesis/{hid}",
        json={"status": "validated", "conclusion": "Confirmed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["status"] == "validated"
    assert body["data"]["conclusion"] == "Confirmed"


def test_patch_invalid_status_400(client: TestClient) -> None:
    created = client.post(
        "/research/hypothesis",
        json={"title": "Init", "thesis": "..."},
    ).json()["data"]
    hid = created["id"]
    resp = client.patch(
        f"/research/hypothesis/{hid}",
        json={"status": "nope"},
    )
    assert resp.status_code == 400


def test_retire_hypothesis(client: TestClient) -> None:
    created = client.post(
        "/research/hypothesis",
        json={"title": "Retire me", "thesis": "..."},
    ).json()["data"]
    hid = created["id"]
    resp = client.post(f"/research/hypothesis/{hid}/retire")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["status"] == "archived"
    assert body["data"]["retired_at"]

    # Default list excludes retired
    resp = client.get("/research/hypothesis")
    assert resp.json()["data"]["count"] == 0

    # include_retired surfaces it
    resp = client.get("/research/hypothesis", params={"include_retired": True})
    assert resp.json()["data"]["count"] == 1


def test_get_hypothesis_404(client: TestClient) -> None:
    resp = client.get("/research/hypothesis/does-not-exist")
    assert resp.status_code == 404


def test_summary_active(client: TestClient) -> None:
    client.post("/research/hypothesis", json={"title": "A", "thesis": "..."})
    client.post("/research/hypothesis", json={"title": "B", "thesis": "..."})
    created = client.post(
        "/research/hypothesis",
        json={"title": "V", "thesis": "..."},
    ).json()["data"]
    client.patch(f"/research/hypothesis/{created['id']}", json={"status": "validated"})

    resp = client.get("/research/hypothesis/summary/active")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["total_active"] == 2
    assert data["counts"]["active"] == 2
    assert data["counts"]["validated"] == 1
    assert len(data["recent_active"]) == 2


def test_create_missing_title_returns_422(client: TestClient) -> None:
    resp = client.post("/research/hypothesis", json={"thesis": "no title"})
    assert resp.status_code == 422
