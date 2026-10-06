-- TD-112 one-off: remove option_surface_iv_daily rows a later re-walk of the same
-- (symbol, trade_date) no longer produced. Owner-gated (rewrites Golden Source rows);
-- the code fix (0.183.0, engines/volatility/surface.py) stops new ones.
--
-- Measured 2026-10-06 on the replica: 122 rows in 98 (symbol, trade_date) groups,
-- trade_date 2026-07-14..2026-09-03, up to 6d 21h older than their group's newest fit.
--
-- Run:  psql -d bifrost_golden_source -v ON_ERROR_STOP=1 -f <this file>
-- It prints the count, deletes inside one transaction, and refuses to commit when the
-- count moved (edit EXPECTED after re-measuring).

\set EXPECTED 122

BEGIN;

CREATE TEMP TABLE td112_stale ON COMMIT DROP AS
SELECT symbol, trade_date, expiry
FROM (
    SELECT symbol, trade_date, expiry, computed_at,
           max(computed_at) OVER (PARTITION BY symbol, trade_date) AS newest
    FROM features.option_surface_iv_daily
) g
WHERE newest - computed_at > interval '1 hour';

SELECT count(*) AS stale_rows, count(DISTINCT (symbol, trade_date)) AS groups FROM td112_stale;
SELECT (count(*) = :EXPECTED) AS count_matches FROM td112_stale \gset
\if :count_matches
    DELETE FROM features.option_surface_iv_daily s
    USING td112_stale t
    WHERE s.symbol = t.symbol AND s.trade_date = t.trade_date AND s.expiry = t.expiry;
    COMMIT;
\else
    \echo 'count moved from EXPECTED; nothing deleted'
    ROLLBACK;
\endif
