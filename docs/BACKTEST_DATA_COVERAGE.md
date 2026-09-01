# Backtest data coverage — what the event engine can and cannot answer

Measured 2026-08-31 against `bifrost_golden_source`. Re-run the queries at the
bottom before relying on these numbers.

## The short version

Stock-leg event studies work today. Option-leg event studies do not, and no
amount of engine work changes that — `raw_market.option_daily` holds under a
month of history, and every option template needs a price on both the entry and
exit date of each historical event.

## Coverage

| Table | Rows | Range | Symbols |
|---|---|---|---|
| `raw_market.option_daily` | 5,539 | **2026-08-05 → 2026-08-31 (26 days)** | 19 |
| `raw_market.stock_daily` | 3,466,925 | 2025-06-02 → 2026-08-31 (15 months) | 14,836 |
| `raw_market.stock_financials` | 591,814 | 2009-02-27 → 2026-08-25 (17 years) | 4,469 |
| `raw_market.corporate_action` | 217 | dividend 196 · split 21 · **earnings 0** | — |

Option data accumulates forward at roughly 350 rows/day. It is not backfilled.

## What that means per template

NVDA, 3-year lookback, earnings events from `stock_financials.filing_date`:

| Template | Priced | Skipped | Why |
|---|---|---|---|
| `long_stock_event` | 5 | 7 | the 7 predate `stock_daily`'s 15-month window |
| `long_atm_straddle` | 0 | 12 | no `option_daily` row covers any of those dates |

The UI reports this split rather than showing an empty result as a 0% win rate.

## Backfilling option history

The machinery exists. `option_contract` accepts `expired=true` and enumerates
historical contracts from Polygon; `option_daily` fetches per-contract daily
bars. Both are in `SUPPORTED_BACKFILL_KINDS` in the market-data plugin. Nothing
needs to be built.

The constraint is the Polygon plan. `market-data-config` in
`plugin-market-data` sets `tier: starter`.

| | starter | developer |
|---|---|---|
| Rate | 5 req/min (7,200/day) | 100 req/s |

Contract density, measured over the 26 days on hand: **NVDA 292 distinct
contracts, TSLA 313**. Weekly expiries roll, so a year is on the order of
2,000–4,000 contracts per symbol, and each contract is one aggregates request.

| Scope | starter | developer |
|---|---|---|
| 1 symbol × 1 year | ~10 hours | minutes |
| 19 symbols × 1 year | **~8 days** | ~1 hour |
| 19 symbols × 3 years | **~3–4 weeks** | ~3 hours |

The starter figures assume the full quota goes to backfill. It does not — the
daily ingest draws on the same 5 req/min, so a backfill of that length either
starves live ingest or takes proportionally longer.

## The decision

Three ways forward, and this one is the Owner's:

1. **Upgrade the Polygon plan.** Costs money; makes the whole option backtest
   surface usable in an afternoon.
2. **Backfill a narrow slice on starter.** A couple of symbols over a few
   months, accepting the ingest contention. Enough to validate the path, not
   enough for statistics.
3. **Stay on stock-leg templates.** Free, works now, answers whether the
   underlying moves around an event — but says nothing about structure, IV
   crush, or premium capture.

Until one is chosen, do not describe option backtests as available.

## Re-measuring

```sql
SELECT 'option_daily' AS t, count(*), min(bar_date)::text, max(bar_date)::text,
       count(DISTINCT underlying)
FROM raw_market.option_daily
UNION ALL
SELECT 'stock_daily', count(*), min(bar_date)::text, max(bar_date)::text,
       count(DISTINCT symbol)
FROM raw_market.stock_daily;
```

```sql
SELECT underlying, count(DISTINCT option_ticker) AS contracts
FROM raw_market.option_daily GROUP BY 1 ORDER BY 2 DESC LIMIT 5;
```

Run against `bifrost_golden_source`; the plugin's tier lives in the
`market-data-config` ConfigMap in the `plugin-market-data` namespace.
