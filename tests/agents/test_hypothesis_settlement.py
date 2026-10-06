"""settles_on — the date the outcome rule settles a hypothesis (0.168.0, plan decision #16)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api import hypothesis as hypothesis_api
from bifrost_research.api.app import create_app
from bifrost_research.copilot.agents import hypothesis_settlement as hs
from bifrost_research.db import calendar as cal

TODAY = date(2026, 10, 5)  # Monday
THANKSGIVING = date(2026, 11, 26)


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.rows: list[Any] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.queries.append(" ".join(sql.split()))
        ids = set(params[0]) if params and isinstance(params[0], list) else set()
        if "us_market_holiday" in sql:
            start, end = params
            self.rows = [(d,) for d in sorted(self.conn.holidays) if start <= d <= end]
        elif "raw_market.stock_daily" in sql:
            self.rows = []  # no bars: the past half of the calendar marks nothing
        elif "research.objective_run" in sql:
            self.rows = [(rid, oid) for rid, oid in self.conn.runs.items() if rid in ids]
        elif "research.objective " in sql:
            self.rows = [(oid, pol) for oid, pol in self.conn.objectives.items() if oid in ids]
        elif "research.candidate_pool" in sql:
            self.rows = [(cid, td) for cid, td in self.conn.candidates.items() if cid in ids]
        elif "research.candidate_outcome" in sql:
            self.rows = [(cid, h, d) for (cid, h), d in self.conn.outcomes.items() if cid in ids]
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchall(self) -> list[Any]:
        return list(self.rows)


class _Conn:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.holidays: set[date] = {THANKSGIVING, date(2026, 12, 25)}
        self.runs: dict[str, str] = {}
        self.objectives: dict[str, Any] = {}
        self.candidates: dict[str, date | None] = {}
        self.outcomes: dict[tuple[str, int], date] = {}

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _fresh_calendar_cache() -> None:
    cal._CLOSED_CACHE.clear()


def _hyp(hid: str, *, cid: str | None = "cand_1", oid: str | None = "obj-x", run: str | None = None, **kw: Any) -> dict[str, Any]:
    ref: dict[str, Any] = {}
    if cid:
        ref["candidate_id"] = cid
    if oid:
        ref["objective_id"] = oid
    if run:
        ref["run_id"] = run
    return {"id": hid, "status": "active", "retired_at": None, "origin_ref": ref or None, **kw}


# --------------------------------------------------------------------------- #
# the calendar arithmetic (shared with the engine's window)
# --------------------------------------------------------------------------- #


def test_settlement_session_enters_on_the_first_session_and_skips_holidays() -> None:
    closed = {THANKSGIVING}
    # Friday entry, 5 sessions: 23 24 25 (26 closed) 27 30.
    assert cal.settlement_session(date(2026, 11, 20), 5, closed) == date(2026, 11, 30)
    assert cal.settlement_session(date(2026, 11, 20), 5, set()) == date(2026, 11, 27)
    # A Sunday candidate enters Monday, like _forward_leg's first bar on or after.
    assert cal.settlement_session(date(2026, 10, 4), 1, set()) == date(2026, 10, 6)
    assert cal.settlement_session(date(2026, 10, 5), 1, set()) == date(2026, 10, 6)
    # Entry itself on a holiday rolls to the next session first.
    assert cal.settlement_session(THANKSGIVING, 1, closed) == date(2026, 11, 30)


# --------------------------------------------------------------------------- #
# every source
# --------------------------------------------------------------------------- #


def test_policy_horizon_is_used_and_labelled() -> None:
    conn = _Conn()
    conn.objectives["obj-x"] = {"resolution": {"horizon_days": 5}}
    conn.candidates["cand_1"] = date(2026, 11, 20)

    got = hs.settlements_for(conn, [_hyp("h1")], today=TODAY)["h1"]

    assert got["settles_on"] == "2026-11-30"  # rolled over Thanksgiving
    assert got["settles_basis"] == {
        "from": "candidate_trade_date",
        "settled": False,
        "reason": None,
        "candidate_id": "cand_1",
        "objective_id": "obj-x",
        "horizon_sessions": 5,
        "source": "policy",
        "trade_date": "2026-11-20",
    }


def test_default_twenty_sessions_without_a_resolution_key() -> None:
    conn = _Conn()
    conn.objectives["obj-x"] = {"universe_mode": "stock_composite"}  # no resolution, as on DEV
    conn.candidates["cand_1"] = date(2026, 10, 2)  # Friday

    got = hs.settlements_for(conn, [_hyp("h1")], today=TODAY)["h1"]

    # 20 sessions after Fri 10-02, no holiday in between: Fri 10-30.
    assert got["settles_on"] == "2026-10-30"
    assert got["settles_basis"]["source"] == "default"
    assert got["settles_basis"]["horizon_sessions"] == 20


def test_default_when_the_objective_is_gone_and_through_the_run() -> None:
    conn = _Conn()
    conn.runs["run_1"] = "obj-run"
    conn.objectives["obj-run"] = {"resolution": {"horizon_days": 1}}
    conn.candidates.update({"cand_1": date(2026, 10, 2), "cand_2": date(2026, 10, 2)})
    rows = [
        _hyp("h-run", cid="cand_1", oid=None, run="run_1"),
        _hyp("h-gone", cid="cand_2", oid="obj-missing"),
    ]

    got = hs.settlements_for(conn, rows, today=TODAY)

    assert got["h-run"]["settles_on"] == "2026-10-05"
    assert got["h-run"]["settles_basis"]["objective_id"] == "obj-run"
    assert got["h-run"]["settles_basis"]["source"] == "policy"
    assert got["h-gone"]["settles_basis"]["source"] == "default"
    assert got["h-gone"]["settles_on"] == "2026-10-30"


def test_no_candidate_lineage() -> None:
    conn = _Conn()
    got = hs.settlements_for(conn, [_hyp("h-manual", cid=None, oid=None)], today=TODAY)
    assert got["h-manual"] == {"settles_on": None, "settles_basis": {"reason": "no_candidate_lineage"}}
    assert conn.queries == []  # nothing to look up


def test_candidate_not_found_and_without_trade_date() -> None:
    conn = _Conn()
    conn.candidates["cand_blank"] = None
    rows = [_hyp("h-missing", cid="cand_gone"), _hyp("h-blank", cid="cand_blank")]

    got = hs.settlements_for(conn, rows, today=TODAY)

    assert got["h-missing"]["settles_on"] is None
    assert got["h-missing"]["settles_basis"]["reason"] == "candidate_not_found"
    assert got["h-missing"]["settles_basis"]["candidate_id"] == "cand_gone"
    assert got["h-blank"]["settles_on"] is None
    assert got["h-blank"]["settles_basis"]["reason"] == "candidate_trade_date_missing"


def test_retired_and_resolved_carry_no_date() -> None:
    conn = _Conn()
    rows = [
        _hyp("h-retired", status="archived", retired_at="2026-09-30T12:00:00+00:00"),
        _hyp(
            "h-won",
            status="validated",
            resolution_json={"resolved_by": "eod_agent", "outcome": {"exit_date": "2026-09-28"}},
        ),
        _hyp("h-lost", status="rejected"),
    ]

    got = hs.settlements_for(conn, rows, today=TODAY)

    assert got["h-retired"] == {"settles_on": None, "settles_basis": {"reason": "retired", "status": "archived"}}
    assert got["h-won"] == {
        "settles_on": None,
        "settles_basis": {"reason": "resolved", "status": "validated", "exit_date": "2026-09-28", "resolved_by": "eod_agent"},
    }
    assert got["h-lost"]["settles_basis"] == {"reason": "resolved", "status": "rejected"}
    assert conn.queries == []


def test_rule_disabled_and_a_horizon_the_engine_never_writes() -> None:
    conn = _Conn()
    conn.objectives.update(
        {"obj-off": {"resolution": {"enabled": False}}, "obj-10": {"resolution": {"horizon_days": 10}}}
    )
    conn.candidates.update({"c1": date(2026, 10, 2), "c2": date(2026, 10, 2)})
    rows = [_hyp("h-off", cid="c1", oid="obj-off"), _hyp("h-10", cid="c2", oid="obj-10")]

    got = hs.settlements_for(conn, rows, today=TODAY)

    assert got["h-off"]["settles_on"] is None
    assert got["h-off"]["settles_basis"]["reason"] == "resolution_disabled"
    assert got["h-10"]["settles_on"] is None
    assert got["h-10"]["settles_basis"]["reason"] == "horizon_not_settled_by_engine"
    assert got["h-10"]["settles_basis"]["engine_horizons"] == [1, 5, 20]


def test_a_settled_outcome_wins_over_the_projection() -> None:
    conn = _Conn()
    conn.candidates["cand_1"] = date(2026, 9, 1)
    conn.outcomes[("cand_1", 20)] = date(2026, 9, 30)  # e.g. a halted day stretched it
    conn.outcomes[("cand_1", 5)] = date(2026, 9, 9)

    got = hs.settlements_for(conn, [_hyp("h1")], today=TODAY)["h1"]

    assert got["settles_on"] == "2026-09-30"
    assert got["settles_basis"]["from"] == "candidate_outcome"
    assert got["settles_basis"]["settled"] is True
    # No projection, so no calendar read.
    assert not any("us_market_holiday" in q for q in conn.queries)


def test_overdue_and_outside_the_engine_window() -> None:
    conn = _Conn()
    conn.candidates.update({"c-late": date(2026, 9, 1), "c-old": date(2026, 6, 1)})
    rows = [_hyp("h-late", cid="c-late"), _hyp("h-old", cid="c-old")]

    got = hs.settlements_for(conn, rows, today=TODAY)

    late = got["h-late"]
    assert late["settles_on"] == "2026-09-29"  # no closures known in the past here
    assert late["settles_basis"]["overdue"] is True
    assert got["h-old"]["settles_on"] is None
    assert got["h-old"]["settles_basis"]["reason"] == "outside_settlement_window"


def test_a_failed_candidate_read_is_a_reason_not_an_error() -> None:
    class _Broken(_Conn):
        def cursor(self) -> _Cursor:
            cur = _Cursor(self)
            real = cur.execute

            def execute(sql: str, params: Any = None) -> None:
                if "candidate_pool" in sql:
                    raise RuntimeError("relation missing")
                real(sql, params)

            cur.execute = execute  # type: ignore[method-assign]
            return cur

    got = hs.settlements_for(_Broken(), [_hyp("h1")], today=TODAY)["h1"]
    assert got["settles_on"] is None and got["settles_basis"]["reason"] == "lookup_failed"


# --------------------------------------------------------------------------- #
# bulk: a constant number of queries, whatever the row count
# --------------------------------------------------------------------------- #


def _populated(n: int) -> tuple[_Conn, list[dict[str, Any]]]:
    conn = _Conn()
    conn.runs["run_1"] = "obj-run"
    conn.objectives.update({"obj-x": {"resolution": {"horizon_days": 5}}, "obj-run": {}})
    rows: list[dict[str, Any]] = []
    for i in range(n):
        cid = f"cand_{i}"
        conn.candidates[cid] = date(2026, 9, 21) + timedelta(days=i % 10)
        if i % 3 == 0:
            conn.outcomes[(cid, 5)] = date(2026, 9, 28)
        rows.append(_hyp(f"h{i}", cid=cid, oid=None if i % 2 else "obj-x", run="run_1" if i % 2 else None))
    return conn, rows


def test_list_reads_in_bulk_not_per_row() -> None:
    small_conn, small = _populated(3)
    big_conn, big = _populated(200)

    hs.settlements_for(small_conn, small, today=TODAY)
    hs.settlements_for(big_conn, big, today=TODAY)

    # runs, objectives, candidates, outcomes, holidays, past bars.
    assert len(small_conn.queries) == len(big_conn.queries) == 6
    assert all(q.count("= ANY(%s)") <= 1 for q in big_conn.queries)


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _Conn, list[dict[str, Any]]]:
    conn = _Conn()
    conn.objectives["obj-x"] = {}
    conn.candidates["cand_1"] = date(2026, 10, 2)
    rows = [_hyp("h1", title="T", symbols=["NVDA"]), _hyp("h-manual", cid=None, oid=None, title="M")]
    monkeypatch.setattr(hypothesis_api, "connect", lambda: conn)
    monkeypatch.setattr(hypothesis_api.repo, "list_hypotheses", lambda c, **k: [dict(r) for r in rows])
    monkeypatch.setattr(
        hypothesis_api.repo, "get_hypothesis", lambda c, hid: next((dict(r) for r in rows if r["id"] == hid), None)
    )
    monkeypatch.setattr(hs, "ny_today", lambda: TODAY)
    return TestClient(create_app()), conn, rows


def test_list_and_get_carry_settles_on(api: tuple[TestClient, _Conn, list[dict[str, Any]]]) -> None:
    client, _conn, _rows = api

    listed = client.get("/research/hypothesis?limit=200")
    assert listed.status_code == 200, listed.text
    by_id = {r["id"]: r for r in listed.json()["data"]["rows"]}
    assert by_id["h1"]["settles_on"] == "2026-10-30"
    assert by_id["h1"]["settles_basis"]["from"] == "candidate_trade_date"
    assert by_id["h1"]["title"] == "T"  # existing fields untouched
    assert by_id["h-manual"]["settles_on"] is None
    assert by_id["h-manual"]["settles_basis"] == {"reason": "no_candidate_lineage"}

    one = client.get("/research/hypothesis/h1")
    assert one.status_code == 200, one.text
    assert one.json()["data"]["settles_on"] == "2026-10-30"


def test_a_derivation_failure_does_not_fail_the_list(
    api: tuple[TestClient, _Conn, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _conn, _rows = api

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("calendar exploded")

    monkeypatch.setattr(hs, "settlements_for", _boom)
    resp = client.get("/research/hypothesis")
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert all(r["settles_on"] is None and r["settles_basis"] == {"reason": "lookup_failed"} for r in rows)
