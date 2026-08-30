#!/usr/bin/env bash
# Verify Data Husbandry scheduler landing (Dagster owns all Golden Source husbandry Cron).
# Authority: bifrost-platform/console dataHusbandryCatalog HUSBANDRY_SCHEDULER_NOTE
# Usage: KUBECONFIG=... ./scripts/verify_husbandry_schedulers.sh
set -euo pipefail

KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/bifrost-k3s.yaml}"
export KUBECONFIG
NS_RESEARCH="${NS_RESEARCH:-research}"
FAIL=0

must_suspend() {
  local ns="$1" name="$2"
  local sus
  sus="$(kubectl -n "$ns" get cronjob "$name" -o jsonpath='{.spec.suspend}' 2>/dev/null || echo MISSING)"
  if [[ "$sus" == "MISSING" ]]; then
    echo "OK   missing CronJob ${ns}/${name} (treated as retired)"
  elif [[ "$sus" != "true" ]]; then
    echo "FAIL ${ns}/${name} suspend=${sus} (must be true — Dagster owns this slot)"
    FAIL=1
  else
    echo "OK   suspend ${ns}/${name}"
  fi
}

echo "== Market-data CronJobs must be suspended =="
for name in \
  market-data-stock-eod \
  market-data-eod-pipeline \
  market-data-universe-daily \
  market-data-corporate \
  market-data-calendar \
  market-data-stock-snapshot \
  market-data-stock-movers \
  market-data-oi-gap-heal \
  market-data-option-bars \
  market-data-minute-bars \
  market-data-option-trades \
  market-data-option-refresh \
  market-data-reference \
  market-data-fundamentals-rotate \
  market-data-related-rotate \
  market-data-maintenance
do
  must_suspend plugin-market-data "$name"
done

echo
echo "== Flex CronJobs must be suspended =="
must_suspend plugin-flex-query flex-query-trades
must_suspend plugin-flex-query flex-query-transactions

echo
echo "== Research CronJobs must be suspended =="
for name in \
  bifrost-analytics-daily \
  research-max-pain \
  research-atm-iv-pcr \
  research-iv-percentile \
  research-engines-momentum \
  research-engines-gex \
  research-engines-iv-surface \
  research-engines-flow \
  research-engines-terrain \
  research-engines-forecast \
  research-scan \
  research-harness \
  research-vrp \
  research-opex-cycle \
  research-vol-surface \
  research-vol-weekly-backfill \
  research-terrain-intraday \
  research-gex-intraday \
  research-settlement \
  research-engines-event-radar \
  research-alert-scan \
  research-signal-hit \
  research-canonical-pnl \
  research-morning-prep \
  research-eod-review \
  research-ensure-partitions
do
  must_suspend "$NS_RESEARCH" "$name"
done

echo
echo "== Dagster deploy =="
if ! kubectl -n "$NS_RESEARCH" get deploy dagster-daemon >/dev/null 2>&1; then
  echo "FAIL dagster-daemon Deployment missing in ${NS_RESEARCH}"
  FAIL=1
else
  ready="$(kubectl -n "$NS_RESEARCH" get deploy dagster-daemon -o jsonpath='{.status.readyReplicas}')"
  if [[ "${ready:-0}" != "1" ]]; then
    echo "FAIL dagster-daemon readyReplicas=${ready:-0}"
    FAIL=1
  else
    echo "OK   dagster-daemon ready"
  fi
fi

echo
echo "== Core schedules must be RUNNING =="
SCHED_OUT="$(
  kubectl -n "$NS_RESEARCH" exec deploy/dagster-daemon -- \
    dagster schedule list -m bifrost_research.orchestration.definitions 2>/dev/null || true
)"
echo "$SCHED_OUT" | grep -E 'Schedule:|STOPPED|RUNNING' || true

must_running() {
  local name="$1"
  if echo "$SCHED_OUT" | grep -q "${name} \[RUNNING\]"; then
    echo "OK   ${name} RUNNING"
  elif echo "$SCHED_OUT" | grep -q "${name} \[STOPPED\]"; then
    echo "FAIL ${name} STOPPED — make dagster-ensure-schedule (or start individually)"
    FAIL=1
  else
    echo "WARN ${name} not listed yet (image may predate multi-schedule migrate)"
  fi
}

must_running research_trading_day_schedule
must_running research_canonical_pnl_schedule
for name in \
  market_snapshot_schedule \
  market_movers_schedule \
  market_reference_schedule \
  market_universe_calendar_schedule \
  market_related_schedule \
  market_option_bars_schedule \
  market_corporate_trades_schedule \
  market_minute_bars_schedule \
  market_fundamentals_rotate_schedule \
  market_option_refresh_schedule \
  market_trim_schedule \
  market_oi_gap_heal_schedule \
  research_vrp_schedule \
  research_opex_schedule \
  research_vol_surface_svi_schedule \
  research_alert_scan_schedule \
  research_signal_hit_schedule \
  research_settlement_schedule \
  research_intraday_schedule \
  research_event_radar_schedule \
  research_morning_prep_schedule \
  research_eod_review_schedule \
  research_ensure_partitions_schedule \
  research_vol_weekly_backfill_schedule
do
  must_running "$name"
done

echo
if [[ "$FAIL" -ne 0 ]]; then
  echo "verify_husbandry_schedulers: FAILED"
  exit 1
fi
echo "verify_husbandry_schedulers: PASSED"
exit 0
