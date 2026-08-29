"""Re-export cohort runner (package root)."""

from bifrost_research.engines.canonical_pnl.compute import (
    clear_canonical_pnl_tables,
    coverage_report,
    fetch_atm_iv_series,
    fetch_spot_series,
    locf_fill_iv,
    run_cohort,
    run_symbol_window,
    upsert_marks,
)

__all__ = [
    "clear_canonical_pnl_tables",
    "coverage_report",
    "fetch_atm_iv_series",
    "fetch_spot_series",
    "locf_fill_iv",
    "run_cohort",
    "run_symbol_window",
    "upsert_marks",
]
