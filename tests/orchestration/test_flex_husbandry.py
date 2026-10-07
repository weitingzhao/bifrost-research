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


# --- flex_ingest_now: the same verdict for a reader outside the batch (TD-244) ---


def test_now_probes_freshness_kpis_and_applies_the_same_rule(monkeypatch) -> None:
    from bifrost_research.orchestration import flex_husbandry as fh

    monkeypatch.setenv("FLEX_QUERY_API_URL", "http://flex.test/")
    urls: list[str] = []
    failed = _kpis(**{"flex-trades": {"last_ok": False, "last_error": "[1003]"}})

    def get(url: str) -> dict:
        urls.append(url)
        return failed

    assert fh.flex_ingest_now(get=get, now=NOW) == flex_ingest_verdict(failed, now=NOW)
    assert urls == ["http://flex.test/flex/dashboard/freshness-kpis"]


def test_now_reads_the_gates_max_age(monkeypatch) -> None:
    from bifrost_research.orchestration import flex_husbandry as fh

    kpis = _kpis(**{"flex-trades": {"last_ok": True, "last_success_at": _ago(30)}})
    monkeypatch.setenv("FLEX_GATE_MAX_AGE_HOURS", "24")
    assert fh.flex_ingest_now(get=lambda _u: kpis, now=NOW)[0] == "stale"
    monkeypatch.delenv("FLEX_GATE_MAX_AGE_HOURS")
    assert fh.flex_ingest_now(get=lambda _u: kpis, now=NOW)[0] == "ok"


def test_now_is_unknown_when_the_probe_never_answers(monkeypatch) -> None:
    from bifrost_research.orchestration import flex_husbandry as fh

    monkeypatch.setattr(fh, "NOW_RETRY_SEC", 0.0)
    calls: list[str] = []

    def down(url: str) -> dict:
        calls.append(url)
        raise ConnectionError("connection refused")

    verdict, reason = fh.flex_ingest_now(get=down, now=NOW)
    assert verdict == "unknown" and "connection refused" in reason
    assert len(calls) == 2  # one retry


def test_now_retries_a_blip(monkeypatch) -> None:
    from bifrost_research.orchestration import flex_husbandry as fh

    monkeypatch.setattr(fh, "NOW_RETRY_SEC", 0.0)
    answers: list[object] = [
        ConnectionError("blip"),
        _kpis(**{"flex-trades": {"last_ok": True, "last_success_at": _ago(2)}}),
    ]

    def flaky(_url: str) -> dict:
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a  # type: ignore[return-value]

    assert fh.flex_ingest_now(get=flaky, now=NOW)[0] == "ok"
