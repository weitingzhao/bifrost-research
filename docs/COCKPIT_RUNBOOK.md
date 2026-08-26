# Research Cockpit Runbook (Wave RS-E)

End-to-end guide for local Research Cockpit: API · MCP · Trade FE · Morning/EOD agents · Copilot write approval.

**Package**: `bifrost-research` **0.16.0** · Program **Wave RS-E Complete** (E1–E4)

---

## Ports

| Component | Port | Command |
|-----------|------|---------|
| Research API | `:8795` | `make run-api` / `uvicorn bifrost_research.api.app:app --port 8795` |
| Research MCP | `:8796` | `python -m bifrost_research.mcp.server` (SSE `/sse`) |
| Trade FE (Vite) | `:5173` | `cd ../bifrost-trade-frontend && npm run dev:k3s` |

FE acceptance uses local Vite against DEV API (`192.168.10.73:30882`) per D-IL1; for pure Cockpit AI work you may also point Research engine URL at local `:8795` if your FE env supports it.

---

## Prerequisites

1. Golden Source PG reachable (`ANALYTICS_PG_*` / CNPG NodePort).
2. Apply Research DDL (hypothesis · backtest_run · **ai_action_log** · **ai_draft**):

```bash
cd bifrost-research
make db-init-research   # or your usual schema apply entrypoint
```

3. Optional LLM keys (chat without keys returns a streamed error; tests never need live keys):

| Env | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` | Claude provider |
| `OPENAI_API_KEY` | OpenAI provider |
| `COPILOT_DAILY_CAP_USD` | Daily cost cap (default **2.0**) |
| `COPILOT_APPROVAL_HMAC_SECRET` | HMAC for write approval tokens (**required in prod**; missing → insecure local/dev fallback) |
| `BIFROST_MORNING_AGENT_DRY_RUN` / `BIFROST_EOD_AGENT_DRY_RUN` | Force agent dry-run when set to `1`/`true` |

---

## Start stack (local)

```bash
# Terminal A — Research API
cd bifrost-research && make run-api

# Terminal B — Research MCP (optional for Cursor Desktop; Copilot uses in-process MCP)
cd bifrost-research && python -m bifrost_research.mcp.server

# Terminal C — Trade FE
cd bifrost-trade-frontend && npm run dev:k3s
# open http://127.0.0.1:5173 → any /research/* page → ⌘K Cockpit
```

### Point Cursor Desktop at MCP

MCP SSE URL: `http://127.0.0.1:8796/sse`  
Transport: HTTP + SSE (D-RS-E-h). Tools include 25+ read tools and 4 write tools (dry_run default).

---

## Morning / EOD agents

**On-demand (Cockpit → Actions)** or HTTP:

```bash
# Dry-run (no draft persistence)
curl -s -X POST http://127.0.0.1:8795/research/agents/morning/run \
  -H 'Content-Type: application/json' -d '{"dry_run":true}'

curl -s -X POST http://127.0.0.1:8795/research/agents/eod/run \
  -H 'Content-Type: application/json' -d '{"dry_run":true}'

# Persist drafts → Inbox
curl -s -X POST http://127.0.0.1:8795/research/agents/morning/run \
  -H 'Content-Type: application/json' -d '{}'
```

CronJobs: `k8s/agents/cronjob-morning-prep.yaml` · `cronjob-eod-review.yaml` (06:30 / 16:30 ET weekdays).

Inbox: Cockpit → **Inbox** → Approve / Dismiss. Approvals write `research.ai_action_log` (`executed` / `rejected`).

---

## Copilot chat + interactive write approval (RS-E4)

1. Open Cockpit → **Copilot**.
2. Ask e.g. `Create a hypothesis for NVDA vol crush after earnings`.
3. Model calls `research.hypothesis.create` with **dry_run=true** (orchestrator forces this).
4. FE shows **DiffApprovalCard** (preview + impact).
5. **Approve**:
   - `POST /research/copilot/approve` → short-lived HMAC token (60s, single-use)
   - `POST /research/copilot/execute` → write tool `dry_run=false` + token
   - Result streamed into chat; `ai_action_log` status → `executed`
6. **Reject**:
   - Card dismisses; assistant note “Action rejected by user”
   - Optional `POST /research/copilot/dismiss` → `ai_action_log` `rejected`

Write tools:

| Tool | Diff kind |
|------|-----------|
| `research.hypothesis.create` | `create_hypothesis` |
| `research.hypothesis.patch` | `patch_hypothesis` |
| `research.hypothesis.retire` | `retire_hypothesis` |
| `research.backtest.run_event_query` | `run_backtest` |

Without a valid token, `dry_run=false` → `403: approval token required`.

Token errors: **409** replay · **410** expired · **400** hash mismatch.

---

## Wave RS-F runtime (Copilot v2)

| Item | Value |
|------|-------|
| SDK | `openai-agents` — `Runner.run_streamed` via `copilot/agent_runtime.py` |
| Default model | `deepseek-chat` (`DEEPSEEK_API_KEY`, base `https://api.deepseek.com/v1`) |
| MCP | Unchanged SSE `:8796/sse` — `MCPServerSse` in `copilot/agents/graph.py` |
| Agents | Triage → handoffs (Discovery / Analyze / Validate / Write / Explain / Verdict) |
| SSE extensions | `agent_handoff`, `guardrail` (FE back-compat) |
| D10 guardrails | Pre-check + SDK Input/OutputGuardrail → `error {code: "D10_FREEZE"}` + `ai_action_log.guardrail_reject` |
| Sessions | `research.copilot_session` · `GET/DELETE /research/copilot/sessions` |
| Tracing | stdout + optional JSONL (`RESEARCH_COPILOT_TRACE_JSONL`) · OTLP via `RESEARCH_COPILOT_OTLP_ENDPOINT` |
| FE | Cockpit Settings: Overlay/Dock · Trace panel · Session sidebar · Agent chips |

Legacy orchestrator (`orchestrator_legacy.py`) retained for API tests with injected `copilot_provider`.

Smoke (needs `DEEPSEEK_API_KEY` + MCP running):

```bash
cd bifrost-research && make smoke-agents-sdk
```

Apply DDL (Golden Source):

```bash
make db-init-research
```

---

## Inspect approval / audit

```sql
SELECT id, action_kind, action_source, status, approved_by, created_at, executed_at
FROM research.ai_action_log
ORDER BY created_at DESC
LIMIT 20;

SELECT id, kind, status, scope, created_at
FROM research.ai_draft
WHERE status = 'pending'
ORDER BY created_at DESC;
```

---

## Reset session

- Cockpit Settings → **Clear session** (local chat history + new `session_id`)
- Pins: localStorage key `bifrost.cockpit.pins.v1`
- Usage counters: UTC day bucket in process memory (`GET /research/copilot/usage`)

---

## Verify (CI / agent)

```bash
cd bifrost-research && make lint && make test
cd ../bifrost-trade-frontend && npm run lint && npm run build && npm run check:legacy-css
```

D10: no `place_order` / `ib:operator` / daemon control in `mcp/` or `copilot/` (covered by tests).

---

## Three smoke stories

1. **Morning**: Run Morning Prep → Inbox approve → hypothesis on Home → Copilot “backtest this with long_atm_straddle, 3y” → Diff card → Approve → view run.
2. **EOD**: Run EOD Review → Inbox approve verdict that patches hypothesis → `validated`.
3. **Ad-hoc**: Copilot “any NVDA vol trades warranted this week?” → tool citations → pin answer (Pins tab).

---

## Out of scope

- Autonomous Agent (RS-E5) — deferred
- Live trading / D10 paths — **BLOCKED**
