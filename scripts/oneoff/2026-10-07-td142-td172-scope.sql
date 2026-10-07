-- TD-142 and TD-172 scope counts. Read-only. Measured 2026-10-07 on the
-- bifrost-postgres-3 replica of bifrost_golden_source. Numbers in the comments
-- are that run; re-run the statements before a backfill and stop if they move
-- by more than a small drift.
--
-- TD-142 (long-tenor ATM IV, DTE 91..220, option_daily since 2025-11-10):
--   existing features.option_metric_atm_iv_daily with DTE>90 and atm_iv set:
--     10633 rows, 230 symbols, 43 sessions, 29 expiries, 2026-08-05..2026-10-06
--     NVDA 39 sessions, AAPL 32, SPY 27
--   option_daily bars in band: 20953 bars, 307 symbols, 227 sessions,
--     4548 (symbol, session, expiry) triples, 3883 symbol-days,
--     first bar 2025-11-10, last 2026-10-06
--   triples before 2026-08-05: 1795 (none of these are in the feature table)
--   triples since 2026-08-05: 2753, of which 1177 are missing from the feature table
--   missing feature rows total: 2972
--   Expected new rows if the backfill writes one ATM IV row per missing triple: 2972.
--   The pass should visit the 3883 symbol-days that already have a long-dated
--   bar, with DTE_MAX lifted to 220 for that pass only. Not timed here.
--
-- TD-172 (50-90 DTE hole, 2026-07-06..2026-09-25):
--   research.option_universe: 718 names
--   name-days with any ATM IV row in the window: 37687 across 684 names and 59 sessions
--   name-days without a 50-100 DTE expiry: 29229
--   weekly share of name-days that have one (50-100): 
--     07-06 0.15, 07-13 0.12, 07-20 0.16, 07-27 0.14, 08-03 0.02, 08-10 0.02,
--     08-17 0.24, 08-24 0.35, 08-31 0.17, 09-07 0.33, 09-14 0.43, 09-21 0.59
--   option_daily bars already stored at 50-90 DTE in the window:
--     114748 bars, 26311 tickers, 634 names, 59 sessions
--   healthy reference week 2026-06-22..26 at 50-90 DTE:
--     74485 bars, 23526 tickers, 684 names (about 14897 bars/session, 34 tickers/name)
--   expected bars if that week held for 59 sessions: 74485/5*59 = 878923
--   shortfall: about 764000 bars
--   vendor requests are one fetch_stock_aggs per option_ticker for the whole
--   from/to range, not per session. A 50-90 DTE contract stays in band about
--   29 sessions, so unique tickers ≈ 23526 * (59/29) ≈ 48000 range requests.
--   Upper bound from contracts still listed on/after 2026-09-28 whose expiry
--   is inside 2026-08-25..2026-12-24: 86361 tickers (min expiry actually seen
--   2026-09-28; contracts that expired before that day are not in this count).
--   At the starter soft cap of 8 req/s (market-data-subscription-focus):
--     48000 / 8 ≈ 100 minutes; 86361 / 8 ≈ 3 hours. That is queue time only.

-- TD-142 existing long-tenor feature rows
SELECT count(*) AS rows,
       count(DISTINCT symbol) AS symbols,
       count(DISTINCT trade_date) AS sessions
FROM features.option_metric_atm_iv_daily
WHERE expiry - trade_date > 90
  AND atm_iv IS NOT NULL;

-- TD-172 weekly share (the acceptance query, current values)
WITH d AS (
    SELECT symbol, trade_date,
           bool_or(expiry - trade_date BETWEEN 50 AND 100) AS has_mid
    FROM features.option_metric_atm_iv_daily
    WHERE trade_date BETWEEN DATE '2026-07-06' AND DATE '2026-09-25'
      AND symbol IN (SELECT symbol FROM research.option_universe)
    GROUP BY 1, 2
)
SELECT date_trunc('week', trade_date)::date AS week,
       count(*) AS name_days,
       round(avg(has_mid::int), 2) AS share_50_100
FROM d
GROUP BY 1
ORDER BY 1;
