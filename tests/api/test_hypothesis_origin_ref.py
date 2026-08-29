"""Unit tests for origin_ref soft validation / merge (Wave 13)."""

from bifrost_research.repositories.hypothesis import merge_origin_ref, normalize_origin_ref


def test_normalize_origin_ref_strips_watchlist_key():
    out = normalize_origin_ref({"watchlist_contract_key": "  STK:NVDA  ", "vrp_pct": 90})
    assert out["watchlist_contract_key"] == "STK:NVDA"
    assert out["vrp_pct"] == 90


def test_normalize_allows_extra_keys_and_odd_contract_key():
    # Soft validation — does not raise on non-matching keys
    out = normalize_origin_ref({"watchlist_contract_key": "CUSTOM:FOO", "extra": True})
    assert out["watchlist_contract_key"] == "CUSTOM:FOO"
    assert out["extra"] is True


def test_merge_origin_ref_preserves_and_updates():
    merged = merge_origin_ref(
        {"symbol": "NVDA", "watchlist_contract_key": "STK:NVDA"},
        {"trajectory_summary": {"row_count": 3}},
    )
    assert merged["symbol"] == "NVDA"
    assert merged["watchlist_contract_key"] == "STK:NVDA"
    assert merged["trajectory_summary"]["row_count"] == 3
