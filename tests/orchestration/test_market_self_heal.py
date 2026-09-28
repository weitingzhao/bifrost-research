"""market_self_heal: doctor → heal → drain → recheck, failing only when still critical."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("dagster")

from dagster import build_asset_context

from bifrost_research.orchestration import market_self_heal as msh


GEN = "2026-09-05T00:45:12+00:00"


def _md(result: Any, key: str) -> Any:
    """Metadata is raw when the asset is called directly, wrapped when Dagster runs it."""
    v = result.metadata[key]
    return getattr(v, "value", v)


def test_should_heal_only_with_prescriptions() -> None:
    assert msh.should_heal({"verdict": "critical", "prescriptions": [{"action": "enqueue-slot"}]})
    assert not msh.should_heal({"verdict": "healthy", "prescriptions": []})
    assert not msh.should_heal(
        {"verdict": "critical", "prescriptions": []}
    )  # worker down: not ours to fix


def test_queue_drained_and_outcome() -> None:
    assert msh.queue_drained({"pending": 0, "running": 0})
    assert not msh.queue_drained({"pending": 3, "running": 0})
    assert msh.outcome({"verdict": "critical"}, {"verdict": "healthy"}) == "healed"
    assert msh.outcome({"verdict": "healthy"}, None) == "healthy"
    assert msh.outcome({"verdict": "critical"}, {"verdict": "degraded"}) == "degraded"


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reports: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    doctor_calls: list[tuple[str, Any]] | None = None,
) -> tuple[Any, list[Any]]:
    posts: list[Any] = []
    gets = iter(reports)
    sums = iter(summaries)

    def fake_get(url: str, **kw: Any) -> dict[str, Any]:
        if "/market/doctor" in url:
            if doctor_calls is not None:
                doctor_calls.append((url, kw.get("timeout")))
            return next(gets)
        return next(sums)

    def fake_post(url: str, body: dict[str, Any], **kw: Any) -> dict[str, Any]:
        posts.append((url, body, kw))
        return {
            "ok": True,
            "enqueued": 4,
            "actions": [
                {"action": "enqueue-slot", "slot": "eod-pipeline", "result": {"enqueued": 4}}
            ],
        }

    monkeypatch.setattr(msh, "get_json", fake_get)
    monkeypatch.setattr(msh, "post_json", fake_post)
    monkeypatch.setattr(msh.time, "sleep", lambda s: None)
    monkeypatch.setenv("MARKET_DATA_WRITE_TOKEN", "t")
    monkeypatch.setenv("MARKET_SELF_HEAL_WAIT_SEC", "120")
    result = msh.market_self_heal(build_asset_context())
    return result, posts


def test_healthy_session_does_not_heal(monkeypatch: pytest.MonkeyPatch) -> None:
    result, posts = _run(
        monkeypatch,
        reports=[
            {
                "session": "2026-09-04",
                "generated_at": GEN,
                "verdict": "healthy",
                "summary": "0 · 0 · 12",
                "prescriptions": [],
                "findings": [],
            }
        ],
        summaries=[],
    )
    assert posts == []
    assert _md(result, "outcome") == "healthy"
    assert _md(result, "healed") is False


def test_heals_then_rechecks_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    before = {
        "session": "2026-09-04",
        "generated_at": GEN,
        "verdict": "critical",
        "summary": "1 · 0 · 11",
        "findings": [{"severity": "crit", "title": "Option chain snapshot", "detail": "3/25"}],
        "prescriptions": [
            {
                "action": "enqueue-slot",
                "slot": "eod-pipeline",
                "date": "2026-09-04",
                "force": True,
                "finding_ids": ["x"],
            }
        ],
    }
    after = {
        "session": "2026-09-04",
        "generated_at": GEN,
        "verdict": "healthy",
        "summary": "0 · 0 · 12",
        "findings": [],
        "prescriptions": [],
    }
    result, posts = _run(
        monkeypatch,
        reports=[before, after],
        summaries=[{"pending": 4, "running": 0}, {"pending": 0, "running": 0}],
    )
    assert len(posts) == 1
    assert posts[0][0].endswith("/market/doctor/heal")
    assert posts[0][1] == {"dry_run": False}
    assert posts[0][2]["token_header"] == "X-Market-Data-Write-Token"
    assert _md(result, "outcome") == "healed"
    assert _md(result, "drained") is True
    assert _md(result, "enqueued") == 4


def test_still_critical_after_heal_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    before = {
        "session": "2026-09-04",
        "generated_at": GEN,
        "verdict": "critical",
        "summary": "",
        "findings": [],
        "prescriptions": [{"action": "enqueue-slot", "slot": "eod-pipeline", "finding_ids": ["x"]}],
    }
    after = {
        "session": "2026-09-04",
        "generated_at": GEN,
        "verdict": "critical",
        "summary": "",
        "prescriptions": [],
        "findings": [{"severity": "crit", "title": "Stock daily bars (whole market)"}],
    }
    with pytest.raises(RuntimeError, match="still critical.*Stock daily bars"):
        _run(monkeypatch, reports=[before, after], summaries=[{"pending": 0, "running": 0}])


def test_both_doctor_reads_are_recomputed_and_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain GET /market/doctor is the Plugin's cache — last night's session, or nothing."""
    before = {
        "session": "2026-09-04",
        "generated_at": GEN,
        "verdict": "critical",
        "summary": "",
        "findings": [],
        "prescriptions": [{"action": "enqueue-slot", "slot": "eod-pipeline", "finding_ids": ["x"]}],
    }
    before["computed_ms"] = 21400
    after = {
        "session": "2026-09-04",
        "generated_at": "2026-09-05T00:47:03+00:00",
        "computed_ms": 18900,
        "verdict": "healthy",
        "summary": "",
        "findings": [],
        "prescriptions": [],
    }
    calls: list[tuple[str, Any]] = []
    result, _ = _run(
        monkeypatch,
        reports=[before, after],
        summaries=[{"pending": 0, "running": 0}],
        doctor_calls=calls,
    )
    assert [u.split("/market/doctor", 1)[1] for u, _ in calls] == [
        "?probes=true&refresh=true",
        "?probes=false&refresh=true",
    ]
    # A fresh report under backfill load took 259s on 2026-09-28.
    assert all(t == msh.MARKET_DOCTOR_TIMEOUT_SEC >= 600 for _, t in calls)
    assert _md(result, "generated_at_before") == GEN
    assert _md(result, "generated_at_after") == "2026-09-05T00:47:03+00:00"
    assert _md(result, "doctor_ms_before") == 21400
    assert _md(result, "doctor_ms_after") == 18900


def test_healthy_run_records_when_its_report_was_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    result, _ = _run(
        monkeypatch,
        reports=[
            {
                "session": "2026-09-04",
                "generated_at": GEN,
                "verdict": "healthy",
                "prescriptions": [],
                "findings": [],
            }
        ],
        summaries=[],
        doctor_calls=calls,
    )
    assert len(calls) == 1 and calls[0][0].endswith("?probes=true&refresh=true")
    assert _md(result, "generated_at_before") == GEN


def test_an_empty_report_is_not_a_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache-miss shape: no session, no verdict — must not materialize as "unknown"."""
    empty = {"ok": True, "findings": [], "generated_at": None, "age_sec": None, "computing": True}
    with pytest.raises(RuntimeError, match="no generated_at"):
        _run(monkeypatch, reports=[empty], summaries=[])


def test_schedule_and_whitelist_wired() -> None:
    from bifrost_research.api.orchestration_schedules import HUSBANDRY_SCHEDULE_JOBS
    from bifrost_research.orchestration.schedules import RESEARCH_JOBS, RESEARCH_SCHEDULES

    assert msh.market_self_heal_schedule.cron_schedule == "45 0 * * 2-6"
    assert msh.market_self_heal_schedule in RESEARCH_SCHEDULES
    assert msh.market_self_heal_job in RESEARCH_JOBS
    assert ("market_self_heal_schedule", "market_self_heal_job", "UTC") in HUSBANDRY_SCHEDULE_JOBS


def test_a_second_pass_follows_fundamentals_market() -> None:
    """00:45 runs before ratios and short volume exist; the late pass sees them."""
    from bifrost_research.api.orchestration_schedules import HUSBANDRY_SCHEDULE_JOBS
    from bifrost_research.orchestration.market_slot_schedules import MARKET_SCHEDULES
    from bifrost_research.orchestration.schedules import RESEARCH_SCHEDULES

    late = msh.market_self_heal_late_schedule
    assert late.cron_schedule == "30 5 * * 2-6"
    assert late.job is msh.market_self_heal_job
    assert late in RESEARCH_SCHEDULES
    assert ("market_self_heal_late_schedule", "market_self_heal_job", "UTC") in HUSBANDRY_SCHEDULE_JOBS
    fundamentals = next(s for s in MARKET_SCHEDULES if s.name == "market_fundamentals_market_schedule")
    fund_h, late_h = int(fundamentals.cron_schedule.split()[1]), int(late.cron_schedule.split()[1])
    assert late_h > fund_h
