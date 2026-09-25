# Historical IV Solver Spec (IDS-1)

**Program**: Historical IV Solver (Waves IDS-1 … IDS-6)  
**Package**: `bifrost-research` ≥ 0.35.0  
**Status**: Owner-approved contract (algo + schema + filters)

## Recon snapshot (Golden Source, 2026-08-28)

| Source | Distinct dates | Symbols | Rows | Has IV? |
|--------|---------------:|--------:|-----:|:-------:|
| `raw_market.option_daily` | **16** (2026-08-05 → 08-27) | 19 | 4,818 | No (OHLCV only) |
| `raw_market.option_snapshot` | **~290** (2024-05-30 → 2026-08-27) | 28 | ~590k | **Yes** (Polygon precomputed) |
| `raw_market.stock_daily` | 312 | 14,821 | 3.4M | n/a |
| `features.option_metric_atm_iv_daily` | 84 | 24 | 13k | Yes (`iv_source=snapshot`) |

**Implication**: A pure `option_daily` BS-inversion cannot reach 252 trading days today.
The program therefore uses a **dual-source** write into `features.option_iv_reconstructed_daily`:

1. **`vendor_snapshot`** — project existing Polygon IV from `option_snapshot` (primary depth path)
2. **`ok` / `no_convergence` / `insufficient_inputs`** — Brent BS inversion from `option_daily` OHLCV (fills daily bars + validates solver)

Downstream `atm_iv` / VRP / canonical PnL consume a unified view that prefers reconstructed rows, then falls back to live snapshot.

## Algorithm

| Item | Choice |
|------|--------|
| Root finder | **Brent's method** (bracket `[0.01, 5.0]`, tol `1e-4`, maxiter 100) |
| Pricing | Black–Scholes European, `r = 0` (shared with `engines.backtest.canonical_pnl.bs_price`) |
| Mid price | `option_daily.close` (fallback `(high+low)/2` if close null) |
| Strike filter | `strike ∈ [0.8·spot, 1.2·spot]` |
| Tenor filter | DTE ∈ `[5, 90]` |
| Greeks | `delta` / `gamma` from BS at solved IV (optional; null if no IV) |

### Solver status enum

| `solver_status` | Meaning |
|-----------------|---------|
| `ok` | Brent converged from OHLCV |
| `no_convergence` | Bracket failed / maxiter |
| `insufficient_inputs` | Missing spot/mid/tte or invalid moneyness |
| `vendor_snapshot` | Copied from Polygon `option_snapshot.iv` (not solved) |

## Schema — `features.option_iv_reconstructed_daily`

```
symbol            text        NOT NULL
option_ticker     text        NOT NULL
trade_date        date        NOT NULL
strike            double precision
expiry            date
option_right      text
mid_price         double precision
spot              double precision
tte_years         double precision
iv                double precision
delta             double precision
gamma             double precision
solver_status     text        NOT NULL DEFAULT 'ok'
computed_at       timestamptz NOT NULL DEFAULT now()
PRIMARY KEY (symbol, option_ticker, trade_date)
```

Index: `(symbol, trade_date)`, `(trade_date DESC)`, `(solver_status)` WHERE `iv IS NOT NULL`.

## Unified ATM source

Python helper `fetch_unified_iv_rows_for_date` (and optional SQL view `features.v_atm_iv_unified`):

1. Prefer `option_iv_reconstructed_daily` rows with non-null `iv` for that NY calendar date
2. Else fall back to `raw_market.v_option_snapshot_with_stock` (existing path)

`engines.volatility.atm_iv.compute_atm_iv_for_date` uses the unified fetch; `iv_source` becomes `reconstructed` or `snapshot`.

## Acceptance gates (program)

| Gate | Target |
|------|--------|
| IDS-2 unit | 5 known IV round-trips error &lt; 1e-3 |
| IDS-2 back-check | vs Polygon ATM IV sample, median abs rel error &lt; 3% |
| IDS-3 coverage | reconstructed: distinct dates ≥ 200, symbols ≥ 25 |
| IDS-4 | `canonical_pnl.insufficient_pct` &lt; 0.15 |
| IDS-5 | Signal Health exposes `iv_reconstruction` block; package `0.35.0` |

## 2026-09 correction (research 0.106.0 – 0.111.0)

**The depth path above was fossil data.** Until plugin P3 (2026-09-08) `option_snapshot.snapshot_ts`
was a contract's *last trade*, so the "~290 dates" in the recon table were contracts fetched in
August stamped with the day they last traded. Their `vendor_snapshot` projections carried August IV
against the stamped day's spot; the ATM picker took the nearest priced strike however far out
(PLTR 2026-06-25: strikes 280/310 at spot 107.27 → IV30 2.256). IDS-3's coverage gate was met by
these rows. Raw observation time starts 2026-08-05; 145,096 projected rows before P3 had no raw
observation behind them.

What replaced it:

| Piece | Now |
|-------|-----|
| ATM strike | within ±10% of spot, else the expiry gets no row |
| ATM evidence | an expiry needs two priced contracts near spot, each at ≥ 5 vol (0.111.0: one stale last trade was most of the 280 readings left after the first repair) |
| ATM source | reconstructed rows, plus Brent solved in place from `option_daily` for contracts they lack (±10%, DTE 5–90). Not stored: ATM is the only reader, and storing the universe's two years would be ~23M rows |
| Vendor vs Brent | a contract-day with vendor IV keeps it |
| Daily solver | vendor projection only (0.111.0); Brent is solved where it is read — ATM, and Trade's greeks route — not stored |
| Projection | a snapshot row must be fetched within 3 days of its session (`SNAPSHOT_MAX_FETCH_LAG_DAYS`) |
| IV30 | `atm_iv.iv30_from_expiries`: interpolated within 7–90 DTE; VRP and the IV percentile share it |
| IV percentile | ranks stored `iv_current` (IV30); NULL until 126 sessions |
| Canonical PnL | prices on IV30 too (it took the single expiry nearest 30 days); rebuilt by the repair's last step over its 6-month window |
| Repair | `engines/volatility/iv_history_repair.py` / `k8s/engines/job-iv-history-repair.yaml`: reproject vendor rows from raw, purge pre-P3 rows no post-P3 projection confirmed (reads only this table, so it holds after raw retention), rebuild ATM → percentile → VRP oldest first |
| Retired | `iv_solver_entry.py` and `job-iv-solver-backfill.yaml` (a 252-day, whole-window-in-memory cohort solve that would also have overwritten vendor rows) |

## Out of scope

- Plugin / Polygon API changes (no historical greeks endpoint)
- Dagster prod unblock
- D10 trading paths
