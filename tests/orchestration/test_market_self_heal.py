"""market_self_heal: doctor → heal → drain → recheck, failing only when still critical."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("dagster")

from dagster import build_asset_context

from bifrost_research.orchestration import market_self_heal as msh


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
) -> tuple[Any, list[Any]]:
    posts: list[Any] = []
    gets = iter(reports)
    sums = iter(summaries)

    def fake_get(url: str, **kw: Any) -> dict[str, Any]:
        if "/market/doctor" in url:
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
        "verdict": "critical",
        "summary": "",
        "findings": [],
        "prescriptions": [{"action": "enqueue-slot", "slot": "eod-pipeline", "finding_ids": ["x"]}],
    }
    after = {
        "session": "2026-09-04",
        "verdict": "critical",
        "summary": "",
        "prescriptions": [],
        "findings": [{"severity": "crit", "title": "Stock daily bars (whole market)"}],
    }
    with pytest.raises(RuntimeError, match="still critical.*Stock daily bars"):
        _run(monkeypatch, reports=[before, after], summaries=[{"pending": 0, "running": 0}])


def test_schedule_and_whitelist_wired() -> None:
    from bifrost_research.api.orchestration_schedules import HUSBANDRY_SCHEDULE_JOBS
    from bifrost_research.orchestration.schedules import RESEARCH_JOBS, RESEARCH_SCHEDULES

    assert msh.market_self_heal_schedule.cron_schedule == "45 0 * * 2-6"
    assert msh.market_self_heal_schedule in RESEARCH_SCHEDULES
    assert msh.market_self_heal_job in RESEARCH_JOBS
    assert ("market_self_heal_schedule", "market_self_heal_job") in HUSBANDRY_SCHEDULE_JOBS
