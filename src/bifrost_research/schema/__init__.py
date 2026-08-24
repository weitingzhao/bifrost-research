"""Schema package — market_analytics + research DDL owned by Research."""

from bifrost_research.schema.ddl import (
    MARKET_ANALYTICS_TABLES,
    RESEARCH_TABLES,
    apply_all_ddl,
    apply_market_analytics_ddl,
    apply_research_ddl,
    ensure_month_partitions,
)

__all__ = [
    "MARKET_ANALYTICS_TABLES",
    "RESEARCH_TABLES",
    "apply_all_ddl",
    "apply_market_analytics_ddl",
    "apply_research_ddl",
    "ensure_month_partitions",
]
