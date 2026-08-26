#!/usr/bin/env bash
# Patch OPENAI_API_KEY into research NS secret and restart research-api.
#
# Usage (do NOT paste the key into chat — run locally):
#   export OPENAI_API_KEY='sk-...'
#   ./scripts/sync_openai_secret.sh
#
# Or: make sync-openai-secret  (reads OPENAI_API_KEY from env)

set -euo pipefail

NS="${RESEARCH_NS:-research}"
SECRET="${RESEARCH_SECRET_NAME:-bifrost-research-secrets}"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
  fi
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: OPENAI_API_KEY is not set." >&2
  echo "  export OPENAI_API_KEY='sk-...' && $0" >&2
  exit 1
fi

if ! kubectl get secret "$SECRET" -n "$NS" >/dev/null 2>&1; then
  echo "Secret $SECRET not found in namespace $NS — creating…" >&2
  kubectl -n "$NS" create secret generic "$SECRET" \
    --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY"
else
  kubectl -n "$NS" patch secret "$SECRET" \
    --type merge \
    -p "{\"stringData\":{\"OPENAI_API_KEY\":\"${OPENAI_API_KEY}\"}}"
fi

echo "Restarting research-api so the pod picks up OPENAI_API_KEY…"
kubectl -n "$NS" rollout restart deployment/research-api
kubectl -n "$NS" rollout status deployment/research-api --timeout=180s

echo "OK. Verify:"
echo "  curl -s http://192.168.10.73:30882/api/plugin/research/research/copilot/models | python3 -m json.tool"
echo "  (should list gpt-4o + gpt-4o-mini under available)"
