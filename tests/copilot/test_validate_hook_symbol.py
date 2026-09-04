"""A per-symbol verdict was being decided by a symbol-independent number.

`validate_hypothesis_stock_leg` resolved the symbol, refused to continue without
one — and then ran the backtest with empty params. Every hypothesis was scored
against the same market-wide aggregate. Measured on the live warehouse: the
market-wide record is win_rate 0.3243 over 37 earnings, which rejects
everything, while WT's own record is 0.6 over 5 and SCCO's is 0.5 over 4. The
bug did not mislabel the evidence, it inverted the verdict.
"""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_research.copilot.harness import validate_hook as vh


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def _run_event_query(event_def: Any, template: str, **kw: Any) -> Any:
        seen["event_def"] = event_def
        return {"summary": {"n_events": 5, "win_rate": 0.6}}

    monkeypatch.setattr(vh, "run_event_query", _run_event_query)
    monkeypatch.setattr(
        vh.hyp_repo, "get_hypothesis", lambda conn, hid: {"id": hid, "symbols": ["WT"]}
    )
    monkeypatch.setattr(
        vh.bt_repo,
        "create_run",
        lambda conn, **kw: (seen.update({"stored": kw}), {"id": "bt-test"})[1],
    )
    monkeypatch.setattr(vh.hyp_repo, "patch_hypothesis", lambda conn, hid, patch: None)
    monkeypatch.setattr(
        vh.action_repo, "insert_action", lambda conn, **kw: {"id": "aal-test"}
    )
    monkeypatch.setattr(
        vh.draft_repo,
        "insert_draft",
        lambda conn, **kw: (seen.update({"draft": kw}), {"id": "drf-test"})[1],
    )
    return seen


def test_the_backtest_is_scoped_to_the_hypothesis_symbol(captured: dict[str, Any]) -> None:
    vh.validate_hypothesis_stock_leg(object(), hypothesis_id="hyp-wt")
    assert captured["event_def"].params == {"symbols": ["WT"]}


def test_the_stored_run_records_the_symbol_it_measured(captured: dict[str, Any]) -> None:
    # The 15 rows already in the warehouse carry `params: {}` and are linked to
    # eight different symbols — indistinguishable from a real per-symbol record.
    vh.validate_hypothesis_stock_leg(object(), hypothesis_id="hyp-wt")
    assert captured["stored"]["event_def"] == {"kind": "earnings", "params": {"symbols": ["WT"]}}


def test_a_symbol_record_above_the_bar_is_validated(captured: dict[str, Any]) -> None:
    out = vh.validate_hypothesis_stock_leg(object(), hypothesis_id="hyp-wt")
    assert out["proposed_status"] == "validated"


def test_the_verdict_shows_its_sample_size(captured: dict[str, Any]) -> None:
    # `event_count` has never been a key on the summary, so every verdict read
    # "events=n/a" and hid how thin its evidence was. Four or five earnings is
    # thin enough that the reader has to see it next to the word "validated".
    vh.validate_hypothesis_stock_leg(object(), hypothesis_id="hyp-wt")
    rationale = captured["draft"]["payload"]["rationale"]
    assert "over 5 earnings" in rationale
    assert "n/a" not in rationale
    assert "WT" in rationale


def test_it_still_refuses_a_hypothesis_with_no_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vh.hyp_repo, "get_hypothesis", lambda conn, hid: {"id": hid, "symbols": []})
    out = vh.validate_hypothesis_stock_leg(object(), hypothesis_id="hyp-none")
    assert out["ok"] is False
    assert "no symbol" in out["error"]
