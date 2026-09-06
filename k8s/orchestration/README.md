# Dagster orchestration (Data Husbandry multi-schedule)

Manifests for `dagster-webserver` + `dagster-daemon` in namespace `research`.

- **replicas: 1** — see `dagster.yaml`
- Image tag should match `bifrost-research` package (e.g. `0.50.0-dagster`)
- Schedules default to **RUNNING** on first insert only; flip STOPPED with:

```bash
make dagster-ensure-schedule
```

## Schedule roster (after full migrate)

| Schedule | Cron / TZ | Role |
|----------|-----------|------|
| `research_trading_day_schedule` | `30 22 * * 1-5` ET | Market EOD + Flex enqueue → gate → dbt → engines + scan |
| `research_canonical_pnl_schedule` | `40 23 * * 1-5` UTC | Canonical PnL |
| `market_snapshot_schedule` | `5 21 * * *` UTC | stock-snapshot |
| `market_movers_schedule` | `10 21 * * *` UTC | stock-movers |
| `market_reference_schedule` | `30 21 * * *` UTC | reference |
| `market_universe_calendar_schedule` | `0 22 * * *` UTC (~17:00 America/Chicago) | universe-daily + calendar + stock-eod + eod-pipeline |
| `market_related_schedule` | `30 22 * * *` UTC | related-rotate |
| `market_option_bars_schedule` | `45 22 * * *` UTC | option-bars |
| `market_corporate_schedule` | `0 23 * * *` UTC | corporate — dividends + splits, whole market (option-trades retired until an Options Developer upgrade) |
| `market_minute_bars_schedule` | `15 23 * * *` UTC | minute-bars |
| `market_fundamentals_rotate_schedule` | `0 3 * * *` UTC | fundamentals-rotate |
| `market_fundamentals_market_schedule` | `30 4 * * 2-6` UTC | fundamentals-market — ratios + short data, whole market by date |
| `market_option_refresh_schedule` | `20 */6 * * *` UTC | option-refresh |
| `market_trim_schedule` | `15 2 * * *` UTC | trim (maintenance Cron) |
| `market_oi_gap_heal_schedule` | `0 4 * * 6` UTC | oi-gap-heal |
| `research_vrp_schedule` … | see `research_aux_schedules.py` | VRP / OpEx / SVI / alert / signal-hit / settlement |
| `research_intraday_schedule` | `30 14-20 * * 1-5` UTC | terrain + gex intraday |
| `research_event_radar_schedule` | `*/30 * * * 1-5` UTC | event-radar ingest |
| `research_morning_prep_schedule` / `research_eod_review_schedule` | UTC | agents |
| `research_ensure_partitions_schedule` / `research_vol_weekly_backfill_schedule` | UTC | maintenance |

**Outside Dagster:** IB Gateway / IB Client / realtime WS Deployments only.

**Executors:** Plugin workers + `ops_jobs.*` (HTTP enqueue). Research Python / dbt run in Dagster.

## Landing check

```bash
make verify-husbandry-schedulers
```

All Golden Source husbandry CronJobs must be `suspend: true`. Core schedules must be RUNNING after image roll.

See Ops Console `dataHusbandryCatalog` `HUSBANDRY_SCHEDULER_NOTE` and Research `CLAUDE.md`.


Every market slot asset carries `RetryPolicy(max_retries=3, delay=60, backoff=EXPONENTIAL)`; the `bifrost_run_failure_alert` sensor POSTs `BifrostDagsterRunFailed` to Alertmanager (`ALERTMANAGER_URL`, default kube-prometheus-stack in `monitoring`), which the Bifrost route forwards to the ops-agent webhook.
