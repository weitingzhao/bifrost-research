#!/usr/bin/env bash
# Verify Data Husbandry scheduler landing (Dagster owns all Golden Source husbandry Cron).
#
# Nothing here is a hand-kept list (TD-108: the old one asserted retired schedules
# and only WARNed when one was absent):
#   - CronJobs: every CronJob in the husbandry namespaces must be suspended,
#     except the ones Dagster does not own (ALLOW_ACTIVE).
#   - Schedules: the expected set is research-api's roster
#     (bifrost_research.api.schedule_roster, test-locked to the Dagster
#     definitions); each must be listed by the daemon and RUNNING. A schedule the
#     daemon lists that the roster does not know is reported (the two images can
#     be on different versions).
#
# Usage: KUBECONFIG=... ./scripts/verify_husbandry_schedulers.sh
set -euo pipefail

KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/bifrost-k3s.yaml}"
export KUBECONFIG
NS_RESEARCH="${NS_RESEARCH:-research}"
# CronJobs that stay active on purpose. research-harness is the only research
# CronJob outside Dagster (weekdays 13:30 UTC).
ALLOW_ACTIVE="${ALLOW_ACTIVE:-research-harness}"
FAIL=0

echo "== Husbandry CronJobs must be suspended =="
for ns in plugin-market-data plugin-flex-query "$NS_RESEARCH"; do
  while read -r name sus; do
    [[ -z "$name" ]] && continue
    if [[ " $ALLOW_ACTIVE " == *" $name "* ]]; then
      echo "OK   ${ns}/${name} suspend=${sus:-false} (outside Dagster by design)"
    elif [[ "$sus" == "true" ]]; then
      echo "OK   suspend ${ns}/${name}"
    else
      echo "FAIL ${ns}/${name} suspend=${sus:-false} (must be true — Dagster owns this slot)"
      FAIL=1
    fi
  done < <(kubectl -n "$ns" get cronjob \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.suspend}{"\n"}{end}')
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
echo "== Every roster schedule must be RUNNING =="
EXPECTED="$(
  kubectl -n "$NS_RESEARCH" exec deploy/research-api -- python -c \
    'from bifrost_research.api.schedule_roster import SCHEDULE_ROSTER as R; print("\n".join(s.name for s in R))'
)"
LISTED="$(
  kubectl -n "$NS_RESEARCH" exec deploy/dagster-daemon -- \
    dagster schedule list -m bifrost_research.orchestration.definitions 2>/dev/null \
    | sed -n 's/^Schedule: \([A-Za-z0-9_]*\) \[\([A-Z_]*\)\].*/\1 \2/p'
)"
if [[ -z "$EXPECTED" ]]; then
  echo "FAIL could not read the roster from research-api"
  FAIL=1
fi
if [[ -z "$LISTED" ]]; then
  echo "FAIL dagster schedule list returned nothing"
  FAIL=1
fi

while read -r name; do
  [[ -z "$name" ]] && continue
  state="$(awk -v n="$name" '$1 == n {print $2}' <<<"$LISTED")"
  case "$state" in
    RUNNING) echo "OK   ${name} RUNNING" ;;
    "") echo "FAIL ${name} not listed by the daemon (its image predates the roster?)"; FAIL=1 ;;
    *) echo "FAIL ${name} ${state} — make dagster-ensure-schedule (or start it)"; FAIL=1 ;;
  esac
done <<<"$EXPECTED"

while read -r name state; do
  [[ -z "$name" ]] && continue
  if ! grep -qx "$name" <<<"$EXPECTED"; then
    echo "WARN ${name} [${state}] listed by the daemon but not in research-api's roster"
  fi
done <<<"$LISTED"

echo
if [[ "$FAIL" -ne 0 ]]; then
  echo "verify_husbandry_schedulers: FAILED"
  exit 1
fi
echo "verify_husbandry_schedulers: PASSED"
exit 0
