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
3. Batch auto-accepts research draft kinds only (excludes playbook/morning_brief)

## D10

Advisory only — no Trade DB writes, no `ib:operator:cmd`, no live orders.
