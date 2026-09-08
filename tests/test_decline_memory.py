"""A declined name returns only when something got better.

The loop proposed the same eleven symbols on two consecutive days because a
refusal was never written where the loop reads. These tests pin the rules that
decide whether a refused name has earned another look.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import decline_memory as dm


def snap(**kw: Any) -> dict[str, Any]:
    base = {"sepa_score": 78.0, "path": "SETUP", "grade": "B", "terrain_regime": "neutral"}
    base.update(kw)
    return base


# ── the rules ──────────────────────────────────────────────────────────────


def test_the_same_reading_is_not_a_change() -> None:
    assert dm.changes_since(snap(), snap()) == []


def test_the_score_threshold_is_a_floor_not_a_hint() -> None:
    assert dm.changes_since(snap(sepa_score=78.0), snap(sepa_score=83.0))
    assert dm.changes_since(snap(sepa_score=78.0), snap(sepa_score=82.9)) == []


def test_a_score_that_fell_is_not_news() -> None:
    # A worse version of the name that was refused, not a reason to re-ask.
    assert dm.changes_since(snap(sepa_score=78.0), snap(sepa_score=60.0)) == []


def test_setup_to_pivot_earns_another_look() -> None:
    out = dm.changes_since(snap(path="SETUP"), snap(path="PIVOT"))
    assert [c["rule"] for c in out] == ["path_advanced"]


def test_pivot_to_extended_is_the_setup_running_away() -> None:
    # The buy point is behind it; EXTENDED ranks below SETUP on purpose.
    assert dm.changes_since(snap(path="PIVOT"), snap(path="EXTENDED")) == []
    assert dm.changes_since(snap(path="EXTENDED"), snap(path="PIVOT"))


def test_a_grade_notch_up_counts_and_a_notch_down_does_not() -> None:
    assert dm.changes_since(snap(grade="B"), snap(grade="A"))
    assert dm.changes_since(snap(grade="A"), snap(grade="B")) == []


def test_a_new_event_counts_only_above_the_layer_bar() -> None:
    then = snap()
    assert dm.changes_since(then, snap(event_date="2026-09-20", event_importance=3))
    assert (
        dm.changes_since(
            then, snap(event_date="2026-09-20", event_importance=1), min_event_importance=2
        )
        == []
    )


def test_a_regime_flip_counts_in_either_direction() -> None:
    assert dm.changes_since(snap(terrain_regime="neutral"), snap(terrain_regime="crash_risk"))


def test_nothing_to_compare_is_not_a_change() -> None:
    assert dm.changes_since(None, snap()) == []
    assert dm.changes_since(snap(), None) == []
    assert dm.changes_since({}, {}) == []


# ── the split ──────────────────────────────────────────────────────────────


def _declined(symbol: str, on: str = "2026-09-04", **kw: Any) -> dict[str, Any]:
    return {
        "id": f"cand-{symbol.lower()}-x",
        "symbol": symbol,
        "trade_date": on,
        "status": "dismissed",
        "lens_snapshot": snap(**kw),
    }


def test_a_name_never_declined_passes_untouched() -> None:
    out = dm.suppress_declined(["NVDA"], snapshots_now={"NVDA": snap()}, declines={})
    assert out["symbols"] == ["NVDA"]
    assert out["returning"] == {} and out["suppressed"] == []


def test_a_declined_name_with_nothing_changed_stays_silent() -> None:
    out = dm.suppress_declined(
        ["HALO"], snapshots_now={"HALO": snap()}, declines={"HALO": _declined("HALO")}
    )
    assert out["symbols"] == []
    assert out["suppressed"][0]["reason"] == "declined and nothing material changed"
    assert out["suppressed"][0]["declined_on"] == "2026-09-04"


def test_a_declined_name_that_improved_returns_and_says_why() -> None:
    out = dm.suppress_declined(
        ["HALO"],
        snapshots_now={"HALO": snap(sepa_score=84.0, path="PIVOT")},
        declines={"HALO": _declined("HALO", sepa_score=78.0)},
    )
    assert out["symbols"] == ["HALO"]
    assert out["returning"]["HALO"]["summary"] == "declined 09-04; score 78.0 → 84.0, now PIVOT"
    assert out["returning"]["HALO"]["declined_candidate_id"] == "cand-halo-x"


def test_no_snapshot_to_compare_stays_silent_and_says_so() -> None:
    # Default-deny: re-proposal needs positive evidence, and "we lost the
    # evidence" is not grounds to overrule a refusal.
    out = dm.suppress_declined(
        ["WT"], snapshots_now={}, declines={"WT": {"symbol": "WT", "trade_date": "2026-09-04"}}
    )
    assert out["symbols"] == []
    assert out["suppressed"][0]["reason"] == "declined; no snapshot to compare"


def test_a_name_settled_today_is_silent_whatever_moved() -> None:
    out = dm.suppress_declined(
        ["EE"],
        snapshots_now={"EE": snap(sepa_score=99.0, path="PIVOT")},
        declines={},
        decided_today={"EE": {"symbol": "EE", "status": "dismissed", "trade_date": "2026-09-08"}},
    )
    assert out["symbols"] == []
    assert out["suppressed"][0]["reason"] == "already decided today (dismissed)"


def test_a_name_promoted_today_does_not_come_back_either() -> None:
    out = dm.suppress_declined(
        ["NVDA"],
        snapshots_now={"NVDA": snap()},
        declines={},
        decided_today={"NVDA": {"symbol": "NVDA", "status": "promoted", "trade_date": "2026-09-08"}},
    )
    assert out["symbols"] == [] and "promoted" in out["suppressed"][0]["reason"]


def test_order_is_preserved_and_every_dropped_name_is_named() -> None:
    out = dm.suppress_declined(
        ["A", "B", "C"],
        snapshots_now={"A": snap(), "B": snap(), "C": snap()},
        declines={"B": _declined("B")},
    )
    assert out["symbols"] == ["A", "C"]
    assert [s["symbol"] for s in out["suppressed"]] == ["B"]


def test_the_gate_can_only_narrow() -> None:
    syms = ["A", "B", "C", "D"]
    out = dm.suppress_declined(
        syms,
        snapshots_now={s: snap() for s in syms},
        declines={"B": _declined("B"), "D": _declined("D")},
    )
    assert len(out["symbols"]) <= len(syms)
    assert set(out["symbols"]).issubset(set(syms))
