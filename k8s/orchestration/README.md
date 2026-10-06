# Dagster orchestration (Data Husbandry multi-schedule)

Manifests for `dagster-webserver` + `dagster-daemon` in namespace `research`.

- **replicas: 1** — see `dagster.yaml`
- Image tag should match `bifrost-research` package (e.g. `0.50.0-dagster`)
- Schedules default to **RUNNING** on first insert only; flip STOPPED with:

```bash
make dagster-ensure-schedule
```

## Schedule roster

There is no table here on purpose: three hand-kept copies of the roster (this
README, Ops Console, `scripts/verify_husbandry_schedulers.sh`) drifted apart
(TD-108). The roster is the code, and the places that need it read it:

| Where | What |
|-------|------|
| `src/bifrost_research/orchestration/{schedules,market_slot_schedules,research_aux_schedules}.py` | The ScheduleDefinitions — name, cron, timezone, job, and for market slots the plugin slots each job enqueues |
| `src/bifrost_research/api/schedule_roster.py` | The same roster for research-api (its image has no Dagster); a test fails on any field that differs from the definitions |
| `GET /research/orchestration/status` → `schedules[]` | Live state per schedule: RUNNING / STOPPED, cron, timezone, next tick, last run, `market_slots` (Ops Console reads this) |
| research-api `GET /metrics` | `bifrost_dagster_schedule_*` liveness series behind the `bifrost-research-orchestration` alerts |
| `make verify-husbandry-schedulers` | Every roster schedule listed by the daemon and RUNNING |

**Outside Dagster:** the `research-harness` CronJob (weekdays 13:30 UTC, the only
research CronJob not suspended), and IB Gateway / realtime WS Deployments.

## Landing check

```bash
make verify-husbandry-schedulers
```

Every CronJob in `research`, `plugin-market-data` and `plugin-flex-query` must be `suspend: true` except `research-harness`; every schedule in research-api's roster must be listed by the daemon and RUNNING.

See Ops Console `dataHusbandryCatalog` `HUSBANDRY_SCHEDULER_NOTE` and Research `CLAUDE.md`.


Every market slot asset carries `RetryPolicy(max_retries=3, delay=60, backoff=EXPONENTIAL)`; the `bifrost_run_failure_alert` sensor POSTs `BifrostDagsterRunFailed` to Alertmanager (`ALERTMANAGER_URL`, default kube-prometheus-stack in `monitoring`), which the Bifrost route forwards to the ops-agent webhook.
