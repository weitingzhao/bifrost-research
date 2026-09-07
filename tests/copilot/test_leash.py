"""research-loop-automation D3 — the leash decides per candidate, from the run's own record."""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness.leash import DEFAULT_MIN_SOURCE_HIT_RATE, accept_gate, source_record, split_batch


def _item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "cand_wt",
        "symbol": "wt",
        "agreement": "agree",
        "net_stance": "support",
        "blocked_by_validate": False,
        "evidence": {
            "selection": {"status": "ok", "path": "SETUP", "grade": "A"},
            "track_record": {
                "status": "ok",
                "horizons": [
                    {"horizon_days": 1, "judged": 12, "hit_rate": 0.58},
                    {"horizon_days": 5, "judged": 8, "hit_rate": 0.5},
                    {"horizon_days": 20, "judged": 0, "hit_rate": None},
                ],
            },
        },
    }
    base.update(over)
    return base


def test_the_longest_judged_horizon_is_the_record() -> None:
    rate, judged, horizon = source_record(_item()["evidence"]["track_record"])
    assert (rate, judged, horizon) == (0.5, 8, 5)
    assert source_record({"status": "not_measured"}) == (None, 0, None)
    assert source_record(None) == (None, 0, None)


def test_a_candidate_passes_only_with_all_four_conditions() -> None:
    ok = accept_gate(_item())
    assert ok["accept"] is True and ok["reasons"] == [] and ok["symbol"] == "WT"
    assert ok["source_hit_rate"] == 0.5 and ok["source_horizon_days"] == 5

    assert accept_gate(_item(agreement="dissent"))["reasons"] == ["judges did not agree (dissent)"]
    assert accept_gate(_item(agreement="single"))["reasons"] == ["judges did not agree (single)"]
    assert accept_gate(_item(agreement=None))["reasons"] == ["judges did not agree (no judge record)"]
    assert accept_gate(_item(blocked_by_validate=True, net_stance="oppose"))["reasons"] == ["validate blocked", "net stance oppose"]
    assert accept_gate(_item(net_stance="abstain"))["reasons"] == ["net stance abstain"]

    no_selection = _item()
    no_selection["evidence"]["selection"] = {"status": "not_measured", "reason": "no SEPA row"}
    assert accept_gate(no_selection)["reasons"] == ["selection evidence not measured"]

    unmeasured = _item()
    unmeasured["evidence"]["track_record"] = {"status": "not_measured", "reason": "nothing settled"}
    assert accept_gate(unmeasured)["reasons"] == ["source track record not measured"]

    thin = _item()
    thin["evidence"]["track_record"] = {"status": "ok", "horizons": [{"horizon_days": 1, "judged": 3, "hit_rate": 1.0}]}
    assert accept_gate(thin)["reasons"] == ["source record thin (3 judged at T+1)"]

    weak = _item()
    weak["evidence"]["track_record"] = {"status": "ok", "horizons": [{"horizon_days": 5, "judged": 20, "hit_rate": 0.4}]}
    assert accept_gate(weak)["reasons"] == ["source hit rate 40% < 45% at T+5"]
    assert accept_gate(weak, min_source_hit_rate=0.35)["accept"] is True


def test_split_batch_names_who_passes_and_why_the_rest_stay() -> None:
    items = [_item(), _item(id="cand_rklb", symbol="RKLB", agreement="dissent", net_stance="dissent"), {"not": "a dict"}, "junk"]
    split = split_batch(items)  # type: ignore[arg-type]
    assert split["min_source_hit_rate"] == DEFAULT_MIN_SOURCE_HIT_RATE
    assert split["accepted"] == [{"id": "cand_wt", "symbol": "WT"}]
    assert split["held"] == [{"id": "cand_rklb", "symbol": "RKLB", "reasons": ["judges did not agree (dissent)", "net stance dissent"]}]
    assert split["accepted_ids"] == {"cand_wt"}
