"""TD-170: a slot enqueue logs a summary, never the Plugin's whole job list.

The option-bars slot answered with every queued job (91,431 on 2026-10-06) and
the asset logged that dict on one ~17 MB line, which promtail truncates and Loki
rejects. These tests feed the enqueue a response of that shape and cap the line.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("dagster")

from bifrost_research.orchestration import plugin_batch_assets, plugin_http
from bifrost_research.orchestration.plugin_http import (
    LOG_LINE_MAX_CHARS,
    enqueue_market_slots,
    summarize_result,
    summary_line,
)


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, msg: str, *args: Any) -> None:
        self.lines.append(msg % args if args else msg)

    info = warning = error = debug = _record


class _Context:
    def __init__(self) -> None:
        self.log = _Log()


def _option_bars_response(n_jobs: int) -> dict[str, Any]:
    return {
        "ok": True,
        "slot": "option-bars",
        "target_date": "2026-10-06",
        "symbols": 713,
        "enqueued": n_jobs,
        "deduped": 5,
        "jobs": [
            {
                "kind": "option_daily",
                "payload": {"option_ticker": f"O:XYZ261016C{i:08d}", "underlying": "XYZ"},
                "id": 9_000_000 + i,
                "deduped": False,
                "priority": 4,
            }
            for i in range(n_jobs)
        ],
    }


def test_market_enqueue_logs_a_summary_of_a_huge_job_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_DATA_WRITE_TOKEN", "test-token")
    monkeypatch.setattr(plugin_http, "post_json", lambda *a, **k: _option_bars_response(100_000))
    ctx = _Context()

    out = enqueue_market_slots(ctx, ("option-bars",))  # type: ignore[arg-type]

    assert ctx.log.lines, "the enqueue should log its result"
    assert max(len(line) for line in ctx.log.lines) <= LOG_LINE_MAX_CHARS
    result_line = next(line for line in ctx.log.lines if "result=" in line)
    assert "'enqueued': 100000" in result_line
    assert "'jobs': {'count': 100000, 'first': [9000000, 9000001, 9000002]}" in result_line
    assert out.metadata["enqueued_total"] == 100_000


def test_market_enqueue_failure_message_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_DATA_WRITE_TOKEN", "test-token")
    failed = {**_option_bars_response(50_000), "ok": False, "error": "boom"}
    monkeypatch.setattr(plugin_http, "post_json", lambda *a, **k: failed)

    with pytest.raises(RuntimeError) as exc:
        enqueue_market_slots(_Context(), ("option-bars",))  # type: ignore[arg-type]

    assert len(str(exc.value)) <= LOG_LINE_MAX_CHARS + 200
    assert "'error': 'boom'" in str(exc.value)


def test_flex_enqueue_logs_a_capped_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEX_QUERY_WRITE_TOKEN", "test-token")
    monkeypatch.setattr(plugin_batch_assets, "get_json", lambda *a, **k: {"source": "configmap"})
    monkeypatch.setattr(plugin_batch_assets, "post_json", lambda *a, **k: _option_bars_response(100_000))
    ctx = _Context()

    plugin_batch_assets._enqueue_flex(ctx, slot="flex-trades")  # type: ignore[arg-type]

    assert max(len(line) for line in ctx.log.lines) <= LOG_LINE_MAX_CHARS


def test_summary_keeps_scalars_and_counts_lists() -> None:
    summary = summarize_result({"ok": True, "n": 3, "rows": [1, 2, 3, 4, 5], "nested": {"ids": [{"id": 7}]}})
    assert summary == {
        "ok": True,
        "n": 3,
        "rows": {"count": 5, "first": [1, 2, 3]},
        "nested": {"ids": {"count": 1, "first": [7]}},
    }


def test_summary_line_hard_caps_a_wide_object() -> None:
    wide = {f"k{i}": i for i in range(20_000)}
    assert len(summary_line(wide)) <= LOG_LINE_MAX_CHARS + 40
