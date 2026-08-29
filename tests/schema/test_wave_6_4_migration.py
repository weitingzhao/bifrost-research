"""Wave 6.4 Feature Store schema unify — mapping tests."""

from __future__ import annotations

from bifrost_research.schema.schemas import (
    CANONICAL_FEATURE_TABLES,
    SCHEMA_FEATURES,
    TABLE_OPTION_METRIC_ATM_IV_DAILY,
    TABLE_STOCK_SIGNAL_MOMENTUM_DAILY,
)


def test_canonical_table_constants_use_features_schema() -> None:
    assert TABLE_OPTION_METRIC_ATM_IV_DAILY == "features.option_metric_atm_iv_daily"
    assert TABLE_STOCK_SIGNAL_MOMENTUM_DAILY == "features.stock_signal_momentum_daily"


def test_canonical_feature_table_registry_has_twenty_five_tables() -> None:
    # Wave 6.4 baseline (19) + VRP + Canonical PnL + IDS option_iv_reconstructed_daily
    # + Analyze C.2 stock_signal_playbook_trigger_intraday
    # + Analyze Wave D stock_signal_scan_daily.
    # + Analyze Wave I stock_signal_lens_hit_daily.
    assert len(CANONICAL_FEATURE_TABLES) == 26
    for qualified in CANONICAL_FEATURE_TABLES:
        assert qualified.startswith(f"{SCHEMA_FEATURES}.")
    assert (
        f"{SCHEMA_FEATURES}.stock_signal_playbook_trigger_intraday"
        in CANONICAL_FEATURE_TABLES
    )
    assert f"{SCHEMA_FEATURES}.stock_signal_scan_daily" in CANONICAL_FEATURE_TABLES
    assert f"{SCHEMA_FEATURES}.stock_signal_lens_hit_daily" in CANONICAL_FEATURE_TABLES