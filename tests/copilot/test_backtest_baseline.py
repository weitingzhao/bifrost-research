"""A 29-second answer that never changes, presented as if it were about the batch.

Measured twice back to back at 28.8s and 29.1s, returning an identical
`n_events=37, win_rate=0.3243` — the event definition carries no parameters and
the query never sees the run's candidates. Every run that planned a backtest
paid half a minute to recompute the same market-wide number, and then attached
it to a batch with nothing saying it was market-wide.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from bifrost_research.copilot.harness import backtest_baseline as bb


@pytest.fixture(autouse=True)
def _clear() -> Any:
    bb.clear_baseline_cache()
    yield
    bb.clear_baseline_cache()


def _stub(monkeypatch: Any, calls: dict[str, int], result: Any = None) -> None:
    def _run(event_def: Any, template: str, **kw: Any) -> Any:
        calls["n"] = calls.get("n", 0) + 1
        if isinstance(result, Exception):
            raise result
        return {"summary": {"n_events": 37, "win_rate": 0.3243}}

    monkeypatch.setattr(
        "bifrost_research.engines.backtest.event_query.run_event_query", _run
    )


def test_the_same_day_is_computed_once(monkeypatch: Any) -> None:
    calls: dict[str, int] = {}
    _stub(monkeypatch, calls)
    for _ in range(3):
        bb.template_baseline(object(), today=date(2026, 9, 4))
    assert calls["n"] == 1, "a deterministic 29s query ran more than once for one day"


def test_a_new_day_recomputes(monkeypatch: Any) -> None:
    # Settled earnings history is what moves the answer, and that moves overnight.
    calls: dict[str, int] = {}
    _stub(monkeypatch, calls)
    bb.template_baseline(object(), today=date(2026, 9, 4))
    bb.template_baseline(object(), today=date(2026, 9, 5))
    assert calls["n"] == 2


def test_a_different_template_is_a_different_answer(monkeypatch: Any) -> None:
    calls: dict[str, int] = {}
    _stub(monkeypatch, calls)
    bb.template_baseline(object(), today=date(2026, 9, 4))
    bb.template_baseline(object(), template="short_stock_event", today=date(2026, 9, 4))
    assert calls["n"] == 2


def test_it_says_the_number_is_not_about_this_batch(monkeypatch: Any) -> None:
    # The loop_curator had to work this out in prose — "identical across every
    # symbol … I did not use them to justify bullish execution verdicts".
    calls: dict[str, int] = {}
    _stub(monkeypatch, calls)
    out = bb.template_baseline(object(), today=date(2026, 9, 4))
    assert out["scope"] == "market_wide"
    assert "not a record for the candidates" in out["scope_note"]
    assert out["summary"]["n_events"] == 37


def test_a_failure_is_reported_not_raised(monkeypatch: Any) -> None:
    # A failed baseline must not cost the Owner the batch.
    calls: dict[str, int] = {}
    _stub(monkeypatch, calls, result=RuntimeError("warehouse down"))
    out = bb.template_baseline(object(), today=date(2026, 9, 4))
    assert out["status"] == "not_measured"
    assert "warehouse down" in out["reason"]


def test_a_failure_is_not_cached(monkeypatch: Any) -> None:
    # Pinning "not measured" for the rest of the day over one transient failure
    # would turn a blip into a day without evidence.
    calls: dict[str, int] = {}
    _stub(monkeypatch, calls, result=RuntimeError("blip"))
    bb.template_baseline(object(), today=date(2026, 9, 4))
    bb.template_baseline(object(), today=date(2026, 9, 4))
    assert calls["n"] == 2
