"""Run a script now (S14) and the script report's reads (B7), Owner 2026-10-06."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.engines.pine import build, run_now
from bifrost_research.engines.pine.library import PineScript

_MOD = "bifrost_research.api.pine"
_SRC = '//@version=5\nindicator("t")\nplotshape(close > open, "buy")'


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None
    run_now._reset_for_tests()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(f"{_MOD}.connect", lambda: MagicMock())
    return TestClient(create_app())


# -- run_now -----------------------------------------------------------------------------


def test_a_run_reports_what_the_build_did() -> None:
    seen: dict[str, Any] = {}

    def runner(**kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"scripts": {"mine": {"version": 2, "mode": "rebuild", "rows": 1234, "errors": 2, "error_sample": {"XYZ": "boom"}}}}

    job = run_now.start("mine", runner=runner, background=False)
    assert seen == {"script_ids": ["mine"], "lock": "skip"}
    assert (job.status, job.rows, job.mode, job.errors, job.message) == ("done", 1234, "rebuild", 2, "2 names did not run")
    assert job.finished_at and run_now.latest("mine") is job and run_now.get(job.id) is job


@pytest.mark.parametrize(
    ("report", "status", "message"),
    [
        ({"scripts": {"mine": {"rows": 0, "errors": 0, "skipped": "another build of this script is running"}}}, "skipped", "another build of this script is running"),
        ({"scripts": {}}, "failed", "the script was not built (switched off or removed)"),
        ({"scripts": {"mine": {"rows": 0, "errors": 1, "error_sample": {"*": "pine-runner: URLError"}}}}, "failed", "pine-runner: URLError"),
    ],
)
def test_a_run_that_did_not_build(report: dict[str, Any], status: str, message: str) -> None:
    job = run_now.start("mine", runner=lambda **kw: report, background=False)
    assert (job.status, job.message) == (status, message)


def test_one_run_at_a_time_and_an_exception_is_a_failed_job() -> None:
    import threading

    gate = threading.Event()
    job = run_now.start("a", runner=lambda **kw: gate.wait(5) and {"scripts": {"a": {"rows": 1, "errors": 0}}})
    with pytest.raises(run_now.Busy) as exc:
        run_now.start("b", runner=lambda **kw: {})
    assert exc.value.job is job
    gate.set()
    for _ in range(100):
        if run_now.get(job.id).status != "running":
            break
        threading.Event().wait(0.02)
    assert run_now.get(job.id).status == "done"

    def boom(**kw: Any) -> dict[str, Any]:
        raise RuntimeError("database unavailable")

    assert run_now.start("b", runner=boom, background=False).status == "failed"


def test_the_build_skips_a_script_whose_lock_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    class Cur:
        def __init__(self) -> None:
            self.sql = ""

        def __enter__(self) -> "Cur":
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            self.sql = sql
            calls.append(sql.split("(")[0].strip())

        def fetchone(self) -> tuple[bool]:
            return (False,)

    calls: list[str] = []

    class Conn:
        def cursor(self) -> Cur:
            return Cur()

        def close(self) -> None:
            return None

    s = PineScript(id="mine", name="m", source=_SRC, version=1)
    monkeypatch.setattr(build, "connect", lambda: Conn())
    monkeypatch.setattr(build, "ensure_builtins", lambda conn: 0)
    monkeypatch.setattr(build, "list_scripts", lambda conn, active_only: [s])
    monkeypatch.setattr(build, "load_symbols_from_env_or_query", lambda conn, symbols: ["AAA"])
    monkeypatch.setattr(build, "built_versions", lambda conn: {})
    monkeypatch.setattr(build, "_build_script", lambda *a, **k: pytest.fail("must not build without the lock"))
    out = build.run(lock="skip")
    assert out["scripts"]["mine"]["skipped"] == "another build of this script is running"
    assert calls == ["SELECT pg_try_advisory_lock"]


# -- API ---------------------------------------------------------------------------------


@pytest.fixture
def lib(monkeypatch: pytest.MonkeyPatch) -> dict[str, PineScript]:
    scripts = {
        "mine": PineScript(id="mine", name="Mine", source=_SRC, version=2, origin="user"),
        "off": PineScript(id="off", name="Off", source=_SRC, version=1, origin="user", is_active=False),
    }
    monkeypatch.setattr(f"{_MOD}.get_script", lambda conn, sid: scripts.get(sid))
    return scripts


def test_run_endpoints(api: TestClient, lib: dict[str, PineScript], monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(f"{_MOD}.run_now.start", lambda sid: started.append(sid) or run_now.RunJob(id="pr-1", script_id=sid))
    r = api.post("/research/pine/scripts/mine/run")
    assert r.status_code == 202 and r.json()["data"]["id"] == "pr-1" and started == ["mine"]
    assert api.post("/research/pine/scripts/off/run").status_code == 400
    assert api.post("/research/pine/scripts/nope/run").status_code == 404

    def busy(sid: str) -> run_now.RunJob:
        raise run_now.Busy(run_now.RunJob(id="pr-0", script_id="other"))

    monkeypatch.setattr(f"{_MOD}.run_now.start", busy)
    r = api.post("/research/pine/scripts/mine/run")
    assert r.status_code == 409 and r.json()["run"]["script_id"] == "other"

    monkeypatch.setattr(f"{_MOD}.run_now.get", lambda jid: run_now.RunJob(id=jid, script_id="mine", status="done", rows=9) if jid == "pr-1" else None)
    assert api.get("/research/pine/runs/pr-1").json()["data"]["rows"] == 9
    assert api.get("/research/pine/runs/pr-9").status_code == 404
    monkeypatch.setattr(f"{_MOD}.run_now.latest", lambda sid: None)
    assert api.get("/research/pine/scripts/mine/run").json()["data"] is None


def test_a_save_of_an_active_new_or_edited_script_starts_a_run(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    current: dict[str, PineScript | None] = {"v": None}
    monkeypatch.setattr(f"{_MOD}.get_script", lambda conn, sid: current["v"])
    monkeypatch.setattr(f"{_MOD}.client.lint", lambda src: [])
    monkeypatch.setattr(f"{_MOD}.upsert_script", lambda conn, s: PineScript(**{**s.__dict__, "version": 1 if current["v"] is None else current["v"].version + (s.source != current["v"].source)}))
    started: list[str] = []
    monkeypatch.setattr(f"{_MOD}.run_now.start", lambda sid: started.append(sid) or run_now.RunJob(id="pr-2", script_id=sid))
    r = api.put("/research/pine/scripts/mine", json={"name": "Mine", "source": _SRC})
    assert r.status_code == 200 and r.json()["data"]["run"]["id"] == "pr-2" and started == ["mine"]
    # same source, still active: nothing to rebuild
    current["v"] = PineScript(id="mine", name="Mine", source=_SRC, version=1)
    r = api.put("/research/pine/scripts/mine", json={"name": "Renamed", "source": _SRC})
    assert "run" not in r.json()["data"] and started == ["mine"]
    # switched off: no run
    current["v"] = None
    r = api.put("/research/pine/scripts/mine", json={"name": "Mine", "source": _SRC, "is_active": False})
    assert "run" not in r.json()["data"]


def test_summary_counts_by_month_and_name(lib: dict[str, PineScript], monkeypatch: pytest.MonkeyPatch) -> None:
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchone.return_value = (date(2025, 1, 6), date(2026, 10, 2), 7, 3, 2)
    cur.fetchall.side_effect = [
        [("2025-01", "buy", 2), ("2025-01", "sell", 1), ("2026-10", "buy", 4)],
        [("AAA", "buy", 4, date(2026, 10, 2)), ("AAA", "sell", 1, date(2025, 1, 20)), ("BBB", "buy", 2, date(2025, 1, 6))],
    ]
    conn = MagicMock()
    conn.cursor.return_value = cur
    monkeypatch.setattr(f"{_MOD}.connect", lambda: conn)
    monkeypatch.setattr(f"{_MOD}.run_now.latest", lambda sid: None)
    d = TestClient(create_app()).get("/research/pine/scripts/mine/summary").json()["data"]
    assert (d["first"], d["last"], d["signals"], d["names"], d["built_version"]) == ("2025-01-06", "2026-10-02", 7, 3, 2)
    assert d["by_month"] == [{"month": "2025-01", "buy": 2, "sell": 1}, {"month": "2026-10", "buy": 4, "sell": 0}]
    assert d["by_name"] == [
        {"symbol": "AAA", "buy": 4, "sell": 1, "last": "2026-10-02"},
        {"symbol": "BBB", "buy": 2, "sell": 0, "last": "2025-01-06"},
    ]
    assert d["script"]["id"] == "mine" and "source" not in d["script"]


def test_signal_stats_passes_detail(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(f"{_MOD}.stats.signal_stats", lambda conn, **kw: seen.update(kw) or {"signals": 0, "by_horizon": {}})
    api.get("/research/pine/signal-stats?script=mine&detail=true")
    assert seen["detail"] is True
    api.get("/research/pine/signal-stats?script=mine")
    assert seen["detail"] is False
