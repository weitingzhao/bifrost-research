{{ config(materialized='table') }}

-- Human-read mart mirroring features.stock_signal_canonical_pnl_daily
-- (Python engine is the write authority; dbt rebuilds for docs/tests).
-- Wave Canonical-PnL Foundation.

select
    as_of_date,
    entry_date,
    symbol,
    structure,
    params_hash,
    structure_params,
    entry_spot,
    entry_atm_iv,
    entry_mid,
    as_of_spot,
    as_of_atm_iv,
    mtm_value,
    pnl_since_entry,
    dte_remaining,
    expired,
    final_pnl,
    data_quality,
    computed_at
from {{ source('features', 'stock_signal_canonical_pnl_daily') }}
