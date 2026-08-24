"""SEPA (Specific Entry Point Analysis) fusion engine.

Fuses four dimensions into a unified daily SEPA score:

* **Fundamental** (F) — reads ``dw_stock.mart_sepa_fundamental_eval`` (8 Minervini
  fundamental criteria + F&R ratios / short-interest signals when available).
* **Trend Template** (T) — computes Minervini's 8 technical criteria directly
  from ``market.stock_daily`` (self-contained; does not depend on dbt).
* **Momentum** (M) — reads ``features.stock_signal_momentum_daily`` composite score
  (Bifrost Momentum Radar; falls back to neutral 50 when missing).
* **Options Structure** (O) — combines IV percentile, PCR, and GEX wall
  proximity from ``features.option_metric_*`` / ``features.option_metric_gex_levels_daily``.

Composite ``sepa_score`` = 0.30·F + 0.35·T + 0.20·M + 0.15·O.

Also emits Weinstein-style ``stage`` (STAGE_1 / STAGE_2A / STAGE_2B / STAGE_2C /
STAGE_3 / STAGE_4) and a discrete ``path`` (SETUP / PIVOT / EXTENDED / WATCH /
AVOID) so downstream product pages can filter directly.

D10 BLOCKED — advisory writes to ``features.stock_signal_sepa_daily`` only.
"""

from bifrost_research.engines.sepa.score import (
    compute_sepa_for_date,
    compute_sepa_for_symbol,
)

__all__ = ["compute_sepa_for_date", "compute_sepa_for_symbol"]
