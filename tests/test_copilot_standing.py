"""The Copilot's standing is a read over three ledgers, scoped to today."""

from __future__ import annotations

from datetime import UTC, datetime

from bifrost_research.copilot import standing


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def test_brief_headline_skips_headings_and_tables() -> None:
    md = "# Daily digest\n\n| a | b |\n- bullet\nTwo memos wait; judges split on NVDA.\nmore"
    assert standing.brief_headline(md) == "Two memos wait; judges split on NVDA."
    assert standing.brief_headline(None) == ""


def test_brief_today_returns_only_todays_digest(monkeypatch) -> None:
    rows = [
        {"id": "d_old", "status": "approved", "created_at": "2020-01-01T10:00:00+00:00", "payload": {"markdown": "# x\nOld."}},
        {"id": "d_new", "status": "pending", "created_at": f"{_today()}T11:30:00+00:00", "payload": {"markdown": "# Digest\nFresh.", "model": "deepseek-chat"}},
    ]
    from bifrost_research.repositories import ai_draft

    monkeypatch.setattr(ai_draft, "list_drafts", lambda conn, **kw: rows)
    out = standing.brief_today(object(), _today())
    assert out == {
        "draft_id": "d_new",
        "status": "pending",
        "created_at": f"{_today()}T11:30:00+00:00",
        "headline": "Fresh.",
        "model": "deepseek-chat",
    }


def test_sessions_today_counts_and_trims(monkeypatch) -> None:
    from bifrost_research.repositories import copilot_session

    rows = [
        {"id": i, "title": f"s{i}", "updated_at": f"{_today()}T0{i}:00:00+00:00", "model": "m", "messages": [{}] * i}
        for i in range(1, 6)
    ] + [{"id": 9, "title": None, "updated_at": "2020-01-01T00:00:00+00:00", "model": "m", "messages": None}]
    monkeypatch.setattr(copilot_session, "list_recent", lambda conn, **kw: rows)
    out = standing.sessions_today(object(), "owner", _today())
    assert out["today"] == 5
    assert [r["title"] for r in out["recent"]] == ["s1", "s2", "s3"]
    assert out["recent"][0]["turns"] == 1


def test_approvals_today_by_status(monkeypatch) -> None:
    from bifrost_research.repositories import ai_action_log

    def fake(conn, *, status, limit):
        if status == "proposed":
            return [{"created_at": f"{_today()}T01:00:00+00:00"}, {"created_at": "2020-01-01T00:00:00+00:00"}]
        if status == "executed":
            return [{"created_at": f"{_today()}T02:00:00+00:00"}]
        return []

    monkeypatch.setattr(ai_action_log, "list_actions", fake)
    out = standing.approvals_today(object(), _today())
    assert out == {"proposed": 1, "approved": 0, "executed": 1, "rejected": 0, "error": 0}


def test_standing_is_fail_soft(monkeypatch) -> None:
    from bifrost_research.repositories import (
        ai_action_log,
        ai_draft,
        copilot_bridge,
        copilot_session,
    )

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ai_draft, "list_drafts", boom)
    monkeypatch.setattr(copilot_session, "list_recent", boom)
    monkeypatch.setattr(ai_action_log, "list_actions", boom)
    monkeypatch.setattr(copilot_bridge, "usage_stats_today", boom)
    out = standing.copilot_standing(object(), owner_id="owner")
    assert out["brief"] is None
    assert out["sessions"] == {"today": 0, "recent": []}
    assert out["approvals"]["proposed"] == 0
    assert out["usage"]["bridge_count_today"] == 0
    assert "cap_usd" in out["usage"]
