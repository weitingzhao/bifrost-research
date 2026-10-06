"""husbandry_gate: the Market side is judged on a freshly computed doctor report.

TD-94: the gate fails closed. A probe that raised left the verdict 'unknown',
which passed (09-16, 09-25, 09-26), and nothing tied the doctor's session to the
one being closed (09-22 passed on 09-18's 'healthy', 09-24 on 09-22's).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("dagster")

from dagster import build_asset_context

from bifrost_research.orchestration import plugin_batch_assets as pba

GEN = "2026-09-05T02:30:21+00:00"


def _flex_ok() -> dict[str, Any]:
    """The gate ages Flex against the wall clock, so the fixture must be recent."""
    recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"dimensions": [{"kind": "flex-trades", "last_ok": True, "last_success_at": recent}]}


def _md(result: Any, key: str) -> Any:
    v = result.metadata[key]
    return getattr(v, "value", v)


HEALTHY = {
    "session": "2026-09-04",
    "generated_at": GEN,
    "verdict": "degraded",
    "eod_critical": {"verdict": "healthy", "detail": ""},
}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    doctor: dict[str, Any] | Exception,
    *,
    flex: dict[str, Any] | Exception | None = None,
    summary: dict[str, Any] | Exception | None = None,
    expected: date = date(2026, 9, 4),
    config: pba.HusbandryGateConfig | None = None,
) -> tuple[Any, list[tuple[str, Any]]]:
    calls: list[tuple[str, Any]] = []

    def answer(value: Any) -> dict[str, Any]:
        if isinstance(value, Exception):
            raise value
        return value

    def fake_get(url: str, **kw: Any) -> dict[str, Any]:
        calls.append((url, kw.get("timeout")))
        if "/market/doctor" in url:
            return answer(doctor)
        if url.endswith("/flex/config/summary"):
            return answer(summary if summary is not None else {"source": "secret"})
        return answer(flex if flex is not None else _flex_ok())

    monkeypatch.setattr(pba, "get_json", fake_get)
    monkeypatch.setattr(pba, "expected_session", lambda: expected)
    monkeypatch.setattr(pba, "PROBE_RETRY_SEC", 0.0)
    return pba.husbandry_gate(build_asset_context(), config or pba.HusbandryGateConfig()), calls


def test_the_doctor_read_is_recomputed_and_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain GET /market/doctor is the Plugin's cache — an earlier session, or nothing."""
    result, calls = _run(
        monkeypatch,
        {
            "session": "2026-09-04",
            "generated_at": GEN,
            "verdict": "degraded",
            "eod_critical": {"verdict": "healthy", "detail": ""},
        },
    )
    doctor = [(u, t) for u, t in calls if "/market/doctor" in u]
    assert len(doctor) == 1 and doctor[0][0].endswith("/market/doctor?probes=false&refresh=true")
    assert doctor[0][1] == pba.MARKET_DOCTOR_TIMEOUT_SEC
    assert _md(result, "market_session") == "2026-09-04"
    assert _md(result, "market_generated_at") == GEN
    assert _md(result, "gate") == "pass"


def test_a_critical_eod_session_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="session 2026-09-04 incomplete"):
        _run(
            monkeypatch,
            {
                "session": "2026-09-04",
                "generated_at": GEN,
                "verdict": "critical",
                "eod_critical": {"verdict": "critical", "detail": "open interest 0/25"},
            },
        )


def test_every_probe_failing_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) Doctor and both Flex probes raise: the gate used to pass on 'unknown'."""
    down = ConnectionError("connection refused")
    with pytest.raises(RuntimeError, match="fails closed") as err:
        _run(monkeypatch, down, flex=down, summary=down)
    assert "Market EOD verdict unknown" in str(err.value)
    assert "Flex ingest unknown" in str(err.value)


def test_a_doctor_that_does_not_answer_blocks_after_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="Market EOD verdict unknown") as err:
        _run(monkeypatch, ConnectionError("reset by peer"))
    assert "reset by peer" in str(err.value)


def test_a_doctor_timeout_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The doctor already had MARKET_DOCTOR_TIMEOUT_SEC; a second 600 s wait buys nothing."""
    with pytest.raises(RuntimeError, match="Market EOD verdict unknown"):
        _run(monkeypatch, TimeoutError("timed out"))
    doctor_calls: list[str] = []

    def fake_get(url: str, **kw: Any) -> dict[str, Any]:
        if "/market/doctor" in url:
            doctor_calls.append(url)
            raise TimeoutError("timed out")
        return {"source": "secret"} if url.endswith("/flex/config/summary") else _flex_ok()

    monkeypatch.setattr(pba, "get_json", fake_get)
    with pytest.raises(RuntimeError):
        pba.husbandry_gate(build_asset_context(), pba.HusbandryGateConfig())
    assert len(doctor_calls) == 1


def test_a_transient_probe_failure_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def flaky_flex() -> dict[str, Any]:
        attempts.append("flex")
        if len(attempts) == 1:
            raise ConnectionError("blip")
        return _flex_ok()

    def fake_get(url: str, **kw: Any) -> dict[str, Any]:
        if "/market/doctor" in url:
            return HEALTHY
        if url.endswith("/flex/config/summary"):
            return {"source": "secret"}
        return flaky_flex()

    monkeypatch.setattr(pba, "get_json", fake_get)
    monkeypatch.setattr(pba, "expected_session", lambda: date(2026, 9, 4))
    monkeypatch.setattr(pba, "PROBE_RETRY_SEC", 0.0)
    result = pba.husbandry_gate(build_asset_context(), pba.HusbandryGateConfig())
    assert _md(result, "gate") == "pass" and len(attempts) == 2


def test_flex_without_dimensions_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="Flex ingest unknown"):
        _run(monkeypatch, HEALTHY, flex={"dimensions": []})


def test_the_doctor_must_have_judged_the_session_being_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) 09-22 passed on 09-18's 'healthy'."""
    with pytest.raises(RuntimeError, match="doctor judged session 2026-09-04, the batch closes 2026-09-08"):
        _run(monkeypatch, HEALTHY, expected=date(2026, 9, 8))


def test_a_report_without_generated_at_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) No generated_at: not known to be a fresh computation."""
    with pytest.raises(RuntimeError, match="no generated_at"):
        _run(monkeypatch, {**HEALTHY, "generated_at": None})


def test_a_hand_run_may_let_unknown_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    down = ConnectionError("connection refused")
    result, _ = _run(
        monkeypatch, down, config=pba.HusbandryGateConfig(allow_unknown=True)
    )
    assert _md(result, "gate") == "pass (allow_unknown)"
    assert "Market EOD verdict unknown" in _md(result, "overridden")


def test_a_hand_run_may_name_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = _run(
        monkeypatch,
        HEALTHY,
        expected=date(2026, 9, 8),
        config=pba.HusbandryGateConfig(expected_session="2026-09-04"),
    )
    assert _md(result, "expected_session") == "2026-09-04" and _md(result, "gate") == "pass"


def test_critical_still_wins_over_unknown_flex(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="incomplete"):
        _run(
            monkeypatch,
            {**HEALTHY, "eod_critical": {"verdict": "critical", "detail": "open interest 0/25"}},
            flex=ConnectionError("down"),
        )
