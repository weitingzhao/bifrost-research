# Alerting (Wave 4)

Elementary can push Slack / Teams alerts after `dbt test` via:

```bash
edr monitor --slack-webhook "$SLACK_WEBHOOK_URL" --profiles-dir . -t "${DBT_TARGET:-dev}"
```

Optional CronJob env (not enabled by default):

| Env | Purpose |
|-----|---------|
| `SLACK_WEBHOOK_URL` | Incoming webhook for failed Elementary / dbt tests |
| `ELEMENTARY_ALERTS` | Set to `1` to run `edr monitor` after report generation |

When enabled, append to `scripts/run_dbt.sh` after `edr report`:

```bash
if [ "${ELEMENTARY_ALERTS:-0}" = "1" ] && [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
  edr monitor --slack-webhook "$SLACK_WEBHOOK_URL" --profiles-dir . -t "${DBT_TARGET:-dev}" \
    || echo "Elementary monitor alerts failed (non-blocking)"
fi
```

Ops Platform Alertmanager / Prometheus rules remain the cluster-wide path; Elementary Slack is an optional analytics-only channel.
