# Loop Orchestrator Runbook — Waves LO-0…LO-4 + LS Stock-first

Operational guide for Research Mode 3 (Harness + CuratorRun + optional Trust L0 batch).

## Prerequisites

Choose one primary universe path:

| Mode | Prerequisite table / pipeline |
|------|------------------------------|
| **stock_composite** (recommended) | `features.stock_signal_sepa_daily` (+ optional momentum / event radar) |
| **scan_legacy** | `features.stock_signal_scan_daily` (Dagster scan asset or Cron) |

- `research-api` `:8795` and optional `research-mcp` `:8796`
- DEV seed:
  - Stock-first: `python scripts/seed_loop_objective.py --apply --profile stock`
  - Legacy scan: `python scripts/seed_loop_objective.py --apply --profile scan`

## Environment

| Variable | Purpose |
|----------|---------|
| `BIFROST_HARNESS_LLM_PLAN=1` | Enable LLM plan step in harness |
| `BIFROST_LOOP_OBJECTIVE_ID` | Cron single-objective override (e.g. `obj-daily-loop-stock`) |
| `DEEPSEEK_API_KEY` | LLM plan + CuratorRun |
| `BIFROST_CURATOR_MODEL` | Curator model (default `deepseek-chat`) |
| `BIFROST_CURATOR_SKIP_AGENT=1` | Skip agent (tests / no API key) |
| `BIFROST_LOOP_BATCH_MODE=1` | Cron batch pipeline |
| `PLATFORM_API_URL` | Trust matrix probe (default `http://127.0.0.1:8780`) |
| `BIFROST_LOOP_TRUST_L0_OVERRIDE=1` | Dev-only skip trust gate |
| `BIFROST_PERSONA_EVAL_AGENTS=1` | Enable LLM Persona eval (default = **heuristic**) |
| `BIFROST_PERSONA_EVAL_SKIP_AGENT=1` | Force heuristic even if agents env set |
| `BIFROST_PERSONA_EVAL_TIMEOUT_S` | Whole-batch agent budget (default 90) |
| `TRADE_API_MONITOR_URL` | Optional — heuristic portfolio overlay via Trade `/status` |

## Persona eval mode (default heuristic)

| Mode | When | What Owner sees |
|------|------|-----------------|
| **heuristic** (default) | Env unset / CI / local | Deterministic analyze→portfolio→validate→verdict from SEPA + track_record (+ best-effort holdings). Reproducible. |
| **agent** | `BIFROST_PERSONA_EVAL_AGENTS=1` | Headless LLM specialists via MCP. Fail-soft → `heuristic_fallback` per symbol. |
| **agent + fallback** | Agents enabled but timed out / no key | Trace `mode=agent` + `fallback_used=true`. UI labels this clearly — not multi-agent success. |

**Do not** put `BIFROST_PERSONA_EVAL_AGENTS=1` in production defaults or Cron YAML for CI-like batch runs. Turn it on for STG Owner-attended dry runs when DeepSeek + `research-mcp` are healthy and you want holdings-aware LLM portfolio judgment.

Trust L0 + batch mode auto-approves **research drafts only** (`candidate_batch` / `hypothesis_suggestion` / `eod_verdict`) when Persona eval has no validate block / dissent. Never auto-approves `policy_suggestion` or `order_intent`. D10 — research loop only, no live orders.

## Two spines — Policy × Personas (0.65.0)

| Spine | Owns | Where |
|-------|------|--------|
| **Policy** | What to pick (funnel) | `objective.policy_json` / Policy templates |
| **Personas** | How to judge | Agent Personas + headless `analyze→portfolio→validate→verdict` |

Default Objective persona = `loop_curator`. Discover assist (`discovery_assist.enabled`) is an optional plugin at funnel exit — it never replaces `resolve_universe`. Toggle + `max_veto_fraction` live on Policy Template editor (syncs into JSON).

Portfolio heuristic tries Trade monitor holdings when `TRADE_API_MONITOR_URL` (or in-cluster DNS) is reachable; otherwise stance=`abstain` with summary **holdings not applied**.

## Policy modes (LS-1)

### Stock-first (`universe_mode: stock_composite`)

```json
{
  "universe_mode": "stock_composite",
  "layers": {
    "sepa": { "stage": ["SETUP", "PIVOT"], "min_score": 70, "required": true },
    "momentum": { "grade": "A", "required": false },
    "events": { "min_importance": 2, "within_days": 5, "required": false }
  },
  "option_overlay": { "enabled": true, "required": false, "flag_filter": "iv_rank:hot" },
  "max_candidates": 8,
  "use_llm_plan": true,
  "auto_validate": true
}
```

- **Stock layers are Required/Optional** per `layers.*.required`.
- **Option overlay**: when `required: false`, missing IV/VRP/GEX does **not** drop symbols.

### Legacy scan (`universe_mode: scan_legacy`)

```json
{
  "universe_mode": "scan_legacy",
  "preset": "adaptive_30d",
  "flag_filter": [],
  "min_composite_score": 0.55,
  "min_hit_rate": 0.45,
  "max_candidates": 8,
  "use_llm_plan": true,
  "auto_validate": true
}
```

## CLI

```bash
python -m bifrost_research.copilot.harness.entry --schedule=daily_open --dry-list
python -m bifrost_research.copilot.harness.entry --schedule=daily_open --require-scan-fresh
python -m bifrost_research.copilot.harness.entry --schedule=daily_open --require-sepa-fresh
python -m bifrost_research.copilot.harness.entry --objective-id=obj-daily-loop-stock --curate-after
python -m bifrost_research.copilot.harness.entry --schedule=daily_open --batch-mode
```

## API

```bash
curl -X POST http://127.0.0.1:8795/research/objectives/{id}/run
curl http://127.0.0.1:8795/research/objective-runs/{run_id}
curl -X POST http://127.0.0.1:8795/research/objective-runs/{run_id}/curate
curl -X POST http://127.0.0.1:8795/research/objective-runs/{run_id}/approve-all
```

White-box pipeline UI: Trade FE `/research/loop/runs/{run_id}`.

## Cron (DEV)

`k8s/engines/cronjob-harness.yaml` — set `BIFROST_LOOP_OBJECTIVE_ID=obj-daily-loop-stock` when stock-first is primary.

## Trust L0 batch (LO-4)

1. Ops Console → Trust & Autonomy → promote **research-loop-batch** to L0
2. Set `BIFROST_LOOP_BATCH_MODE=1` on Cron Job
3. Batch auto-accepts research draft kinds only (excludes playbook/morning_brief); skips batches with Persona dissent / validate block
4. Keep Persona eval on **heuristic** for Cron unless Owner explicitly enables agents for that STG job

## DEV acceptance (heuristic smoke)

```bash
# From bifrost-research (editable install)
pytest tests/copilot/test_persona_eval_chain.py -q

# If research-api is up (:8795) and an objective exists:
curl -s http://127.0.0.1:8795/health
curl -s -X POST http://127.0.0.1:8795/research/objectives/<obj_id>/run
# Observe run.outputs.persona_eval.mode == "heuristic"
# FE: Harness Console → run row → Pipeline Persona panel / Inbox CandidateBatch mode badge
```

Do **not** set `BIFROST_PERSONA_EVAL_AGENTS=1` for this smoke unless MCP + LLM are intentionally under test.

## D10

Advisory only — no Trade DB writes, no `ib:operator:cmd`, no live orders.
