# Playbook Trigger Rollout Runbook — Analyze Wave E

**Status:** DEV Agent-owned · STG/PROD Owner-gated  
**Package:** bifrost-research ≥ **0.37.0** (shipped as **0.38.0** with Waves D/F)  
**D10:** BLOCKED — triggers are advisory event-log only  

## Preconditions

1. DEV has run `research-engines-intraday` (or forecast upsert path) ≥ 3 times after image roll
2. `features.stock_signal_playbook_trigger_intraday` exists and has rows for current session symbols
3. `GET /research/playbook/hit-rate?symbol=SPY&window_days=30` returns JSON (hit_rate may be null until enough forwards settle)
4. Optional gate: observed hit_rate ≥ 0.40 over ≥ 10 evaluated dominant triggers (Owner may waive)

## DEV (Agent)

```bash
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/bifrost-k3s.yaml}"

# 1) Apply DDL Job (idempotent apply_all_ddl)
kubectl -n research delete job research-ddl-apply-analyze-def --ignore-not-found
kubectl apply -f k8s/jobs/ddl-apply.yaml
kubectl -n research wait --for=condition=complete job/research-ddl-apply-analyze-def --timeout=180s

# 2) Roll Cron images (terrain-intraday + forecast emit triggers; scan materializes daily)
kubectl -n research set image cronjob/research-terrain-intraday \
  compute=192.168.10.73:30500/bifrost-research:0.38.0
kubectl -n research set image cronjob/research-engines-forecast \
  compute=192.168.10.73:30500/bifrost-research:0.38.0
kubectl apply -f k8s/engines/cronjob-scan.yaml

# 3) Verify
kubectl -n research get cronjob | grep -E 'terrain-intraday|forecast|scan'
# After next intraday tick:
# SELECT COUNT(*) FROM features.stock_signal_playbook_trigger_intraday WHERE trade_date = CURRENT_DATE;
# curl -s "$RESEARCH_API/research/playbook/triggers?symbol=SPY"
```

## STG → PROD (Owner)

1. Confirm DEV smoke ≥ 1 trading day and FE Intraday Playbook timeline shows events
2. STG: same DDL Job + image roll; watch 24h
3. PROD: same steps during a low-risk window
4. Flip spine `D-Playbook-Live` from `PENDING_STG` → `SIGNED` after PROD verify

## Rollback

```bash
kubectl -n research set image cronjob/research-engines-intraday \
  compute=192.168.10.73:30500/bifrost-research:0.36.0
```

DDL tables are additive (`IF NOT EXISTS`) — leave in place; readers tolerate empty trigger tables.

## Acceptance

| Check | Pass |
|-------|------|
| Playbook timeline non-empty for SPY (session day) | yes |
| Hit-rate KPI renders (number or —) | yes |
| Signal Health shows `playbook_trigger` + `scan` | not missing |
| D10 not unlocked | yes |
