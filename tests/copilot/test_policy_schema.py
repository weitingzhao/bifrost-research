"""LS-1 policy schema tests."""

from __future__ import annotations

from bifrost_research.copilot.harness.policy_schema import (
    default_stock_composite_policy,
    parse_policy,
    validate_policy_for_mode,
)


def test_parse_policy_defaults_scan_legacy() -> None:
    p = parse_policy({})
    assert p.universe_mode == "scan_legacy"
    assert p.max_candidates == 3


def test_parse_stock_composite_round_trip() -> None:
    raw = default_stock_composite_policy()
    p = parse_policy(raw)
    assert p.universe_mode == "stock_composite"
    assert p.layers.sepa.required is True
    assert p.layers.momentum.required is False
    assert p.option_overlay.required is False
    assert p.option_overlay.enabled is True


def test_validate_stock_mode_hit_rate_warning() -> None:
    p = parse_policy({"universe_mode": "stock_composite", "min_hit_rate": 0.5})
    warnings = validate_policy_for_mode(p)
    assert any("min_hit_rate" in w for w in warnings)


def test_deep_merge_layers_in_repo() -> None:
    from bifrost_research.repositories.objective import _deep_merge_policy_patch

    current = {
        "universe_mode": "stock_composite",
        "layers": {"sepa": {"min_score": 70, "required": True}},
    }
    patch = {"layers": {"sepa": {"min_score": 75}}}
    merged = _deep_merge_policy_patch(current, patch)
    assert merged["layers"]["sepa"]["min_score"] == 75
    assert merged["layers"]["sepa"]["required"] is True
