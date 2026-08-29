# Canonical PnL — Owner Sign-off Report
Entry-date anchor: **2026-03-03**  ·  trajectory horizons: `+0d, +7d, +14d, +21d, +30d, +45d`  ·  **observe-only (D10)**
This report exercises the in-memory pricing library `bifrost_research.engines.backtest.canonical_pnl` — no DB / IB / Golden Source access. Sign-off gates: **schema + sample entry PnL sanity**.

## 1. DDL — `features.stock_signal_canonical_pnl_daily` / `dw_stock.mart_canonical_pnl_daily`
Identical shape on both tables (dual-write). PK = `(as_of_date, entry_date, symbol, structure, params_hash)`.

| Column | Type | Notes |
|---|---|---|
| `as_of_date` | date NOT NULL | valuation date |
| `entry_date` | date NOT NULL | anchor / snapshot date |
| `symbol` | text NOT NULL | upper-case |
| `structure` | text NOT NULL | one of 5 canonical |
| `params_hash` | text NOT NULL | md5-16 of canonical params dict |
| `structure_params` | jsonb DEFAULT '{}' | rendered params |
| `entry_spot` | double | close at entry_date |
| `entry_atm_iv` | double | ATM 30d IV at entry |
| `entry_mid` | double | net entry premium (credit +/debit −) |
| `as_of_spot` | double | close at as_of_date |
| `as_of_atm_iv` | double | ATM 30d IV at as_of |
| `mtm_value` | double | current mark of held position |
| `pnl_since_entry` | double | mtm − entry_mid |
| `dte_remaining` | int | 0 at/after expiry |
| `expired` | bool DEFAULT false | |
| `final_pnl` | double | payoff at expiry |
| `data_quality` | text DEFAULT 'ok' | `ok` \| `iv_interpolated` \| `insufficient_chain` |
| `computed_at` | timestamptz DEFAULT now() | write watermark |

Index: `(symbol, entry_date, structure)` on both tables.
Registered in `CANONICAL_FEATURE_TABLES` (registry count → **21**, test updated). `_ensure_tables()` creates schema + tables + indexes idempotently.

## 2. Sample entry pricing (5 structures)
Deterministic Black–Scholes with `r = 0`, ATM IV as decimal, delta-parameterized strikes. Scenarios pick a mixed cohort (mega-cap, mid-cap, high-vol tech, cash-secured put low-priced name).

### `short_strangle` — spot 100.0, IV 0.25
- params: `{'structure': 'short_strangle', 'short_call_delta': 0.15, 'short_put_delta': -0.15, 'dte': 45}`
- params_hash: `95eb5997a1ef4e2d`
- entry_mid (net credit / −debit): **136.59**
- data_quality at entry: `iv_interpolated`

| as_of | spot | dte | pnl_since_entry | expired | quality |
|---|---:|---:|---:|:-:|---|
| 2026-03-03 | 100.00 | 45 | 0.00 |  | `iv_interpolated` |
| 2026-03-10 | 100.40 | 38 | 31.37 |  | `iv_interpolated` |
| 2026-03-17 | 100.80 | 31 | 61.37 |  | `iv_interpolated` |
| 2026-03-24 | 101.20 | 24 | 89.06 |  | `iv_interpolated` |
| 2026-04-02 | 101.60 | 15 | 119.05 |  | `iv_interpolated` |
| 2026-04-17 | 102.00 | 0 | 136.59 | ✓ | `iv_interpolated` |

### `put_credit_spread` — spot 120.0, IV 0.30
- params: `{'structure': 'put_credit_spread', 'short_delta': -0.3, 'width': 5.0, 'dte': 45}`
- params_hash: `0a4b348598e512ca`
- entry_mid (net credit / −debit): **132.94**
- data_quality at entry: `iv_interpolated`

| as_of | spot | dte | pnl_since_entry | expired | quality |
|---|---:|---:|---:|:-:|---|
| 2026-03-03 | 120.00 | 45 | 0.00 |  | `iv_interpolated` |
| 2026-03-10 | 120.80 | 38 | 20.59 |  | `iv_interpolated` |
| 2026-03-17 | 121.60 | 31 | 43.58 |  | `iv_interpolated` |
| 2026-03-24 | 122.40 | 24 | 69.14 |  | `iv_interpolated` |
| 2026-04-02 | 123.20 | 15 | 102.66 |  | `iv_interpolated` |
| 2026-04-17 | 124.00 | 0 | 132.94 | ✓ | `iv_interpolated` |

### `long_straddle` — spot 250.0, IV 0.45
- params: `{'structure': 'long_straddle', 'dte': 30}`
- params_hash: `48125e7696176d8c`
- entry_mid (net credit / −debit): **-2,571.61**
- data_quality at entry: `iv_interpolated`

| as_of | spot | dte | pnl_since_entry | expired | quality |
|---|---:|---:|---:|:-:|---|
| 2026-03-03 | 250.00 | 30 | 0.00 |  | `iv_interpolated` |
| 2026-03-10 | 254.00 | 23 | -279.19 |  | `iv_interpolated` |
| 2026-03-17 | 258.00 | 16 | -557.31 |  | `iv_interpolated` |
| 2026-03-24 | 262.00 | 9 | -822.31 |  | `iv_interpolated` |
| 2026-04-02 | 266.00 | 0 | -971.61 | ✓ | `iv_interpolated` |
| 2026-04-17 | 270.00 | -15 | -571.61 | ✓ | `iv_interpolated` |

### `covered_call` — spot 180.0, IV 0.28
- params: `{'structure': 'covered_call', 'short_call_delta': 0.3, 'dte': 30, 'own_stock': 100}`
- params_hash: `f374c0790a7fc793`
- entry_mid (net credit / −debit): **-17,736.12**
- data_quality at entry: `iv_interpolated`

| as_of | spot | dte | pnl_since_entry | expired | quality |
|---|---:|---:|---:|:-:|---|
| 2026-03-03 | 180.00 | 30 | 0.00 |  | `iv_interpolated` |
| 2026-03-10 | 181.20 | 23 | 146.67 |  | `iv_interpolated` |
| 2026-03-17 | 182.40 | 16 | 304.37 |  | `iv_interpolated` |
| 2026-03-24 | 183.60 | 9 | 481.52 |  | `iv_interpolated` |
| 2026-04-02 | 184.80 | 0 | 743.88 | ✓ | `iv_interpolated` |
| 2026-04-17 | 186.00 | -15 | 863.88 | ✓ | `iv_interpolated` |

### `short_put` — spot 60.0, IV 0.35
- params: `{'structure': 'short_put', 'short_delta': -0.3, 'dte': 30}`
- params_hash: `a6db5fb23b1289b8`
- entry_mid (net credit / −debit): **120.95**
- data_quality at entry: `iv_interpolated`

| as_of | spot | dte | pnl_since_entry | expired | quality |
|---|---:|---:|---:|:-:|---|
| 2026-03-03 | 60.00 | 30 | 0.00 |  | `iv_interpolated` |
| 2026-03-10 | 59.40 | 23 | 7.71 |  | `iv_interpolated` |
| 2026-03-17 | 58.80 | 16 | 19.13 |  | `iv_interpolated` |
| 2026-03-24 | 58.20 | 9 | 37.76 |  | `iv_interpolated` |
| 2026-04-02 | 57.60 | 0 | 120.95 | ✓ | `iv_interpolated` |
| 2026-04-17 | 57.00 | -15 | 99.77 | ✓ | `iv_interpolated` |

## 3. Sanity assertions covered by pytest (`tests/engines/test_canonical_pnl.py`)
- BS put-call parity within tolerance (r=0)
- `strike_for_delta` inverts BS delta within 3 delta units
- Short strangle → entry credit > 0
- Long straddle → net debit
- PnL at entry ≈ 0 (< $5 rounding)
- Trajectory spans populate over multi-date sim
- Missing spot / IV → `data_quality = insufficient_chain` and `pnl = NULL`
- All 5 structures build cleanly

**pytest suite (research):** 358 passed.

## 4. Sign-off checklist
- [ ] DDL fields + PK + index shape approved
- [ ] 5 sample structure PnL rows above look reasonable
- [ ] `insufficient_chain` sentinel + NULL PnL policy approved
- [ ] Dual-write (`features.*` + `dw_stock.*`) accepted
- [ ] Ready to authorize **medium cohort backfill** (6mo × Watchlist∪Benchmarks ≈ 50 syms × 5 structures)

## 5. Production cohort backfill — Golden Source, 2026-08-28
Ran `python -m bifrost_research.engines.canonical_pnl.entry --lookback-months 6` against `bifrost_golden_source` (Watchlist∪Benchmarks universe).

| Metric | Value |
|---|---|
| Universe | **27 symbols** (SPX skipped — no `raw_market.stock_daily`) |
| Entry dates | 69 |
| Structures | 5 canonical |
| Rows written (dual: `features.*` + `dw_stock.*`) | **107,680** |
| `iv_interpolated` (real BS pricing) | 38,710 (**35.9%**) |
| `insufficient_chain` (no IV history for date) | 68,970 (**64.1%**) |
| Bellwether coverage (real pricing %) | SPY 66% · TSLA 58% · NVDA 50% · AAPL 39% · MSFT 23% |
| Wall-clock | ~104 s (single-process, no parallelism) |

**Root cause of `insufficient_chain`:** `features.option_metric_atm_iv_daily` today has only **84 distinct trade_dates** for 24 symbols (2025-06-26 → 2026-08-27, sparse), and `features.stock_signal_vrp_daily` (fallback IV source) has just 4 dates. The 6-month backfill window therefore hits many days without an IV reading, and the engine correctly emits `insufficient_chain` + NULL PnL rather than fabricate a mark.

**Next remediation** (out of scope for this sign-off): extend IV history via Market Data Plugin backfill or shorten cohort lookback to align with actual `option_metric_atm_iv_daily` coverage. `SPY 66%` bellwether coverage is already sufficient for VRP-Lab / Signal-Health smoke.

### Real-data example: SPY short_strangle, entry 2026-08-13
- entry spot: **$777.88**, ATM IV: **20.5%**, entry credit: **$872.78** (2 short options)
- **Day +1** (2026-08-14): spot $776.34, IV 20.7% → PnL **+$11.84** (theta win)
- **Day +4** (2026-08-17): spot $772.67, IV **27.6%** (vol spike) → PnL **−$831.58**

Qualitatively correct: short-vol strategy loses on vol expansion + adverse delta.

## 6. Endpoint smoke (research-api 0.34.0)
- `GET /research/canonical-pnl/structures` → 200, 5 structures
- `GET /research/canonical-pnl/coverage` → 200 with cohort counts above
- `GET /research/canonical-pnl/trajectory?symbol=SPY&structure=short_strangle&entry_date=2026-08-13` → 200 real rows
- `GET /research/signal-health` → `overall=ok`; canonical_pnl `status=fresh`, `rows=107680`, `insufficient_pct=0.64`
- `GET /research/exhibit/composite?symbol=AAPL` → 200, 4 fresh lenses (VRP · IV Rank · Terrain · Order Sentiment)
- `GET /research/exhibit/{lens}?symbol=AAPL` (vrp/iv_rank/terrain/order_sentiment) → 200 each
- `GET /research/similar-regime?lens=vrp&symbol=AAPL&value=<x>` → 200 (k-NN over 96 VRP rows across 24 symbols; small history)

---
_Generated by `scripts/signoff_canonical_pnl.py` — deterministic; re-run any time to regenerate this report._
