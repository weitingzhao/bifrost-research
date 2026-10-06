"""Output checks judge what a runner returned (TD-92).

The thresholds are set against 2026-10-06's metadata: gex 42 failed / 1,366 ok
over two sessions (3%) must pass; the TD-89 / gex_intraday class (0 rows, 90%+
failed, skipped every night) must fail at ERROR.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("dagster")

from dagster import (
    AssetCheckSeverity,
    AssetKey,
    DagsterEventType,
    DagsterInstance,
    asset,
    materialize,
)

from bifrost_research.orchestration import asset_checks as ac
from bifrost_research.orchestration.engine_assets import (
    PER_SYMBOL_SPEC,
    PINE_SPEC,
    SUGGESTION_SPEC,
    VOLATILITY_SPEC,
)
from bifrost_research.orchestration.failure_alerts import asset_check_payload, failed_error_checks

ERROR = AssetCheckSeverity.ERROR
WARN = AssetCheckSeverity.WARN


def _severities(result: dict[str, Any], spec: ac.OutputSpec, history: list[dict[str, Any]] | None = None) -> list:
    findings, _ = ac.judge_output(result, spec, history=history or [])
    return [s for s, _ in findings]


def test_a_normal_gex_night_passes() -> None:
    result = {"rows_written": 410_000, "symbols_ok": 1_366, "symbols_failed": 42}
    assert _severities(result, PER_SYMBOL_SPEC) == []


def test_a_broken_engine_fails_at_error() -> None:
    result = {"rows_written": 3_000, "symbols_ok": 20, "symbols_failed": 649, "failures_by_reason": {"No OI contracts": 649}}
    findings, meta = ac.judge_output(result, PER_SYMBOL_SPEC)
    assert [s for s, _ in findings] == [ERROR]
    assert "No OI contracts" in findings[0][1]
    assert meta["symbols_failed_share"] == pytest.approx(649 / 669, abs=1e-4)


def test_zero_rows_where_rows_are_expected_is_error() -> None:
    assert _severities({"rows_written": 0, "symbols_ok": 0, "symbols_failed": 0}, PER_SYMBOL_SPEC) == [ERROR]


def test_zero_rows_where_zero_is_a_quiet_day_passes() -> None:
    spec = ac.OutputSpec(rows=ac.field("rows_written"), trailing=False)
    assert _severities({"rows_written": 0}, spec) == []


def test_a_skip_warns_with_its_reason_and_twice_is_error() -> None:
    skipped = {"mode": "skipped", "reason": "trade api unavailable: 503", "rows_written": 0}
    spec = ac.OutputSpec(rows=ac.field("rows_written"), trailing=False)
    findings, meta = ac.judge_output(skipped, spec)
    assert [s for s, _ in findings] == [WARN] and meta["skip_reason"] == "trade api unavailable: 503"
    assert _severities(skipped, spec, [{ac.SKIPPED_KEY: True}]) == [ERROR]
    assert _severities(skipped, spec, [{ac.SKIPPED_KEY: False}]) == [WARN]


def test_a_skipped_count_is_not_a_skip() -> None:
    """momentum / vrp carry ``skipped`` as a count of names, not a mode."""
    spec = ac.OutputSpec(rows=ac.field("rows_written"), expect_rows=True)
    assert _severities({"rows_written": 900, "skipped": 4}, spec) == []


def test_rows_far_below_the_trailing_median_warn() -> None:
    spec = ac.OutputSpec(rows=ac.field("rows_written"), expect_rows=True)
    history = [{ac.ROWS_KEY: n} for n in (1000, 980, 1010, 990)]
    assert _severities({"rows_written": 400}, spec, history) == [WARN]
    assert _severities({"rows_written": 900}, spec, history) == []
    # Too little history judges nothing: no false WARN on the first nights.
    assert _severities({"rows_written": 400}, spec, history[:2]) == []


def test_errors_and_ok_false_fail() -> None:
    assert _severities({"ok": False}, ac.GENERIC) == [ERROR]
    assert _severities({"error": "boom"}, ac.OutputSpec(error_severity=WARN)) == [WARN]


def test_names_failing_every_session_past_onboarding_warn() -> None:
    result = {
        "rows_written": 1000,
        "symbols_ok": 1000,
        "symbols_failed": 4,
        "failed_every_session_past_onboarding": ["GRML", "NVR"],
    }
    findings, _ = ac.judge_output(result, PER_SYMBOL_SPEC)
    assert [s for s, _ in findings] == [WARN] and "NVR" in findings[0][1]


def test_volatility_rows_sum_every_slot_and_a_skipped_slot_is_error() -> None:
    result = {
        "slots": [
            {"slot": "max-pain", "rows_written": 600},
            {"slot": "atm-iv-pcr", "atm_rows_written": 600, "pcr_rows_written": 590},
            {"slot": "iv-percentile", "skipped": True, "reason": "no trading days"},
        ]
    }
    findings, meta = ac.judge_output(result, VOLATILITY_SPEC)
    assert meta["rows"] == 1790
    assert [s for s, _ in findings] == [ERROR] and "iv-percentile" in findings[0][1]


def test_pine_runner_down_is_error() -> None:
    result = {"scripts": {"s1": {"rows": 0, "errors": 1, "error_sample": {"*": "pine-runner: ConnectionError"}}}}
    assert _severities(result, PINE_SPEC) == [ERROR]
    assert _severities({"scripts": {"s1": {"rows": 0, "errors": 0, "error_sample": {}}}}, PINE_SPEC) == []


def test_a_caught_pine_issue_error_still_shows() -> None:
    result = {"issued": {"rows": 1}, "pine": {"error": "UndefinedColumn: x"}, "settled": {"rows": 2}}
    assert _severities(result, SUGGESTION_SPEC) == [ERROR]


# --- in a real run -----------------------------------------------------------

_KEY = AssetKey(["engines", "probe"])
_RESULTS: list[dict[str, Any]] = []


@asset(key=_KEY, check_specs=ac.output_check_specs(_KEY))
def _probe(context):  # noqa: ANN001, ANN202 — Dagster reads the live annotation; this module has postponed ones
    result = _RESULTS.pop(0)
    return ac.judged_materialization(context, result, {"rows_written": result["rows_written"]}, PER_SYMBOL_SPEC)


def test_a_failed_check_leaves_the_run_green_and_is_alerted() -> None:
    instance = DagsterInstance.ephemeral()
    _RESULTS[:] = [
        {"rows_written": 1000, "symbols_ok": 1000, "symbols_failed": 30},
        {"rows_written": 0, "symbols_ok": 0, "symbols_failed": 669},
    ]
    good = materialize([_probe], instance=instance)
    bad = materialize([_probe], instance=instance)
    assert good.success and bad.success, "output checks are non-blocking"

    def evaluations(run: Any) -> list[Any]:
        return [
            e.event_specific_data
            for e in run.all_events
            if e.event_type == DagsterEventType.ASSET_CHECK_EVALUATION
        ]

    [ok] = evaluations(good)
    [failed] = evaluations(bad)
    assert ok.passed and not failed.passed and failed.severity == ERROR
    assert failed_error_checks(instance, good.run_id) == []
    [(asset_name, check, findings)] = failed_error_checks(instance, bad.run_id)
    assert (asset_name, check) == ("engines/probe", ac.OUTPUT_CHECK)
    assert "0 rows" in findings and "symbols_failed 669/669" in findings
    [alert] = asset_check_payload("research_trading_day", bad.run_id, [(asset_name, check, findings)])
    assert alert["labels"]["alertname"] == "BifrostDagsterAssetCheckFailed"
    assert alert["labels"]["asset"] == "engines/probe"


def test_history_feeds_the_next_judgement() -> None:
    instance = DagsterInstance.ephemeral()
    _RESULTS[:] = [{"rows_written": 1000, "symbols_ok": 10, "symbols_failed": 0} for _ in range(3)] + [
        {"rows_written": 300, "symbols_ok": 10, "symbols_failed": 0}
    ]
    runs = [materialize([_probe], instance=instance) for _ in range(4)]
    last = [
        e.event_specific_data
        for e in runs[-1].all_events
        if e.event_type == DagsterEventType.ASSET_CHECK_EVALUATION
    ][0]
    assert not last.passed and last.severity == WARN
    assert last.metadata["trailing_median_rows"].value == 1000.0
