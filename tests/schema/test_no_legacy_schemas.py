"""Schema guard tests for canonical Feature Store naming."""

from __future__ import annotations

from bifrost_research.db.schema_guard import FORBIDDEN_LEGACY_SCHEMAS
from bifrost_research.schema.schemas import CANONICAL_FEATURE_TABLES


def test_canonical_feature_tables_use_features_schema() -> None:
    for qualified in CANONICAL_FEATURE_TABLES:
        assert qualified.startswith("features."), f"{qualified} must live in features schema"


def test_forbidden_legacy_schemas_list_is_stable() -> None:
    assert "features_daily" in FORBIDDEN_LEGACY_SCHEMAS
    assert "market_analytics" in FORBIDDEN_LEGACY_SCHEMAS
    assert "features" not in FORBIDDEN_LEGACY_SCHEMAS
