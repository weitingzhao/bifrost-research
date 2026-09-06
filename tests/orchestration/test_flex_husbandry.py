"""The gate's Flex rule: outcome and age, from the plugin's freshness-kpis payload."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bifrost_research.orchestration.flex_husbandry import flex_ingest_verdict

NOW = datetime(2026, 9, 7, 22, 30, tzinfo=UTC)  # Monday evening


def _kpis(**dims: dict) -> dict:
    return {"dimensions": [{"kind": k, **v} for k, v in dims.items()]}


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_ok_when_both_kinds_succeeded_recently() -> None:
    verdict, _ = flex_ingest_verdict(
        _kpis(**{"flex-trades": {"last_ok": True, "last_success_at": _ago(12)}, "flex-transactions": {"last_ok": True, "last_success_at": _ago(12)}}),
        now=NOW,
    )
    assert verdict == "ok"


def test_last_attempt_failed_blocks_and_names_the_error() -> None:
    verdict, reason = flex_ingest_verdict(
        _kpis(**{"flex-trades": {"last_ok": False, "last_error": "not_ready: [1003] Statement is not available.", "last_success_at": _ago(30)}}),
        now=NOW,
    )
    assert verdict == "failed"
    assert "[1003]" in reason and "flex-trades" in reason


def test_saturday_success_is_still_fresh_on_monday_night() -> None:
    verdict, _ = flex_ingest_verdict(_kpis(**{"flex-trades": {"last_ok": True, "last_success_at": _ago(64)}}), now=NOW)
    assert verdict == "ok"
    verdict, reason = flex_ingest_verdict(_kpis(**{"flex-trades": {"last_ok": True, "last_success_at": _ago(100)}}), now=NOW)
    assert verdict == "stale" and "older than 96h" in reason


def test_unknown_without_dimensions_or_success() -> None:
    assert flex_ingest_verdict({}, now=NOW)[0] == "unknown"
    assert flex_ingest_verdict(None, now=NOW)[0] == "unknown"
    verdict, reason = flex_ingest_verdict(_kpis(**{"flex-trades": {"last_ok": None, "last_success_at": None}}), now=NOW)
    assert verdict == "stale" and "never succeeded" in reason
