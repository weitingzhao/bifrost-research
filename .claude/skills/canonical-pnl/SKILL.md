# Canonical PnL Foundation — SKILL

## When to use

Implementing or operating hypothetical canonical structure PnL for Watchlist trust / Analyze replay.

## Commands

```bash
cd bifrost-research
make install-dev   # or pip install -e ".[dev]"
pytest tests/engines/test_canonical_pnl.py -q
python -m bifrost_research.engines.canonical_pnl.entry --dry-run --symbol SPY --lookback-months 1
# after DDL applied:
python -m bifrost_research.engines.canonical_pnl.entry --lookback-months 6
```

API (research-api :8795):

```
GET /research/canonical-pnl/structures
GET /research/canonical-pnl/coverage
GET /research/canonical-pnl/trajectory?symbol=SPY&entry_date=2026-01-15&structure=short_strangle
```

## Files

| Path | Role |
|------|------|
| `engines/backtest/canonical_pnl.py` | BS pricing + 5 structures |
| `engines/canonical_pnl/compute.py` | dual-write + cohort |
| `engines/canonical_pnl/entry.py` | Cron entry |
| `api/canonical_pnl.py` | HTTP |
| `schema/ddl.py` / `schemas.py` | DDL + constants |
| `dbt/models/marts/mart_canonical_pnl_daily.sql` | human-read mart |
| `k8s/engines/cronjob-canonical-pnl.yaml` | schedule |

## Constraints

- D10 BLOCKED — no live orders
- Sparse chain → `data_quality=insufficient_chain`, pnl NULL
- Dual write: features (code) + dw_stock mart (dbt rebuild / Python dual-write)
