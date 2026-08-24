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


def test_canonical_feature_table_registry_has_nineteen_tables() -> None:
    assert len(CANONICAL_FEATURE_TABLES) == 19
    for qualified in CANONICAL_FEATURE_TABLES:
        assert qualified.startswith(f"{SCHEMA_FEATURES}.")
