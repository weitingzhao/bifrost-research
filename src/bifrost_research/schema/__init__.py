"""Schema package — features Feature Store DDL owned by Research."""

from bifrost_research.schema.ddl import (
    MARKET_ANALYTICS_TABLES,
    RESEARCH_TABLES,
    apply_all_ddl,
    apply_features_ddl,
    apply_market_analytics_ddl,
    apply_research_ddl,
    drop_legacy_feature_schemas,
    ensure_month_partitions,
)

__all__ = [
    "MARKET_ANALYTICS_TABLES",
    "RESEARCH_TABLES",
    "apply_all_ddl",
    "apply_features_ddl",
    "apply_market_analytics_ddl",
    "apply_research_ddl",
    "drop_legacy_feature_schemas",
    "ensure_month_partitions",
]
