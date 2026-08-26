# Research Copilot v2 Plan — Wave RS-F (OpenAI Agents SDK · DeepSeek · Multi-Agent)

**Status**: **Planned** — awaiting Owner sign-off on `D-RS-F-a` … `D-RS-F-i` before F1 kickoff
**Predecessor**: Wave RS-E Complete (`docs/plans/RESEARCH_COCKPIT_PLAN.md`) — hand-rolled `orchestrator.py` + Claude/OpenAI/Ollama providers + Research MCP `:8796` SSE + preset agents + HMAC approval
**Successor scope**: Ops / Hermes / Cockpit unification deferred to a separate future wave (Owner decision, 2026-08-25)
**Total duration estimate**: 3–4 weeks (F1–F5 core · F6–F7 optional)
**Cross-repo scope**: `bifrost-research` (SDK migration · agent graph · new session DDL) · `bifrost-trade-frontend` (Cockpit v2 UX · DeepSeek in model catalog · trace panel · dock/overlay toggle)
**Trade execution freeze (D10)**: BLOCKED — every phase preserves the existing hard rule; write tools stay `dry_run=true` in chat and HMAC-gated on `/copilot/execute`.

---

## Program Rationale

RS-E landed a functional Copilot but the orchestrator is 250 LOC of hand-rolled tool loop with three provider adapters, no first-class multi-agent, no built-in tracing/guardrails/sessions, no DeepSeek, and a modal `Sheet` drawer that blocks the underlying page. RS-F rebases the runtime on the **OpenAI Agents SDK** (`openai-agents`) and adds first-class multi-agent handoffs + DeepSeek — the specific things the Owner asked for on 2026-08-25.

**Why now**: SDK is provider-agnostic (accepts DeepSeek via OpenAI-compatible endpoint), supports our existing MCP SSE server unchanged, gives Sessions / Guardrails / Tracing for free, and matches industry patterns for quant copilots (80/20 rule: 80% deterministic MCP tools + 20% LLM planning/composition, hard-coded D10 guardrail).

**Explicitly out-of-scope for RS-F**:
- Ops (`bifrost-platform`) / Nous Hermes / platform MCP — no touch. Ops keeps DeepSeek via Hermes; Cockpit gets its own direct DeepSeek path.
- Any change to live trading path (D10 blocked).
- Autonomous agents (still deferred from RS-E — 2-person team ROI).

---

## Multi-Agent Execution Guide

Every Phase declares:
- `depends_on` — Phases that must complete first
- `can_run_in_parallel_with` — Phases safe to run concurrently
- `files.new` / `files.modify` — exact paths
- `data_model_changes` — DDL touched
- `api_contract_changes` — routes added / modified
- `verify_cmd` — commands the executing agent must run before reporting completion
- `acceptance` — checklist for Owner sign-off
- `sign_off.required` — whether Owner must approve before next Phase in the chain starts

Follow `.cursor/rules/phase-execution.mdc`. In batch mode, sign-off phases still pause for Owner unless explicit pre-authorization is given.

**Parallelization snapshot**:
- F1.1 → F1.2 → F1.3 → F1.4 (strict serial — SDK bootstrap)
- F2.1 → F2.2 → F2.3 (serial — DeepSeek plumbing)
- F3.1 → (F3.2 · F3.3 parallel) → F3.4
- F4.1 → (F4.2 · F4.3 parallel)
- F5.1 → (F5.2 · F5.3 · F5.4 parallel)
- F6.1 → F6.2 (optional)
- F7 (optional / deferred)

---

## Owner Decisions (needed before F1)

| ID | Question | Options | Recommendation |
|----|----------|---------|----------------|
| **D-RS-F-a** | Runtime SDK | (1) **`openai-agents` (Python)** — chosen 2026-08-25<br>(2) Keep hand-rolled orchestrator + only add DeepSeek | **(1) `openai-agents`** — locked by Owner |
| **D-RS-F-b** | MCP transport | (1) Keep current **SSE** (`:8796/sse` unchanged; SDK's `MCPServerSse` supports it)<br>(2) Migrate to `MCPServerStreamableHttp` (MCP recommended, SSE deprecated by upstream)<br>(3) Both, feature-flag | **(1) Keep SSE for RS-F**, plan Streamable HTTP migration in follow-up wave (RS-G-optional) — avoid coupling transport migration with SDK migration |
| **D-RS-F-c** | Default model | (1) `deepseek-chat` (Owner has key; ~10× cheaper)<br>(2) Keep `claude-4.5-sonnet`<br>(3) User preference per-session (localStorage, default DeepSeek) | **(3) User preference · default `deepseek-chat`** — cheapest + Owner-configured; per-session override |
| **D-RS-F-d** | DeepSeek transport | (1) **Direct API** `https://api.deepseek.com/v1` (OpenAI-compatible)<br>(2) Route through Hermes<br>(3) Through OpenRouter | **(1) Direct API** — locked by Owner (2026-08-25) |
| **D-RS-F-e** | Multi-agent topology | (1) Single Agent with rich prompt<br>(2) Triage + 5 specialists (Discovery / Analyze / Validate / Verdict / Write) using **handoffs**<br>(3) Manager Agent + specialists as `Agent.as_tool()` | **(2 + 3 hybrid)** — Triage does handoff routing for chat; Verdict Agent uses specialists as `Agent.as_tool()` for compose. Falls through to single-agent behaviour if triage picks no handoff. |
| **D-RS-F-f** | SSE event schema | (1) Extend current schema (add `agent_handoff` + `guardrail` events; back-compat) — FE parses only new events it knows<br>(2) Bump to v2 endpoint `/research/copilot/stream/v2`<br>(3) Break current schema | **(1) Extend, back-compat** — no FE break; unrecognized events silently ignored |
| **D-RS-F-g** | Session storage | (1) In-memory only (current)<br>(2) New table `research.copilot_session` (jsonb history, TTL 30 d)<br>(3) Redis (ops complexity) | **(2)** — matches D-RS-E-g audit posture; enables "resume last chat" |
| **D-RS-F-h** | Guardrail strictness | (1) **Hard reject** on D10 patterns (input + output)<br>(2) Warn only<br>(3) Model-decides | **(1) Hard reject** — Pydantic-enforced patterns; refuses `place_order`, `daemon.start`, `daemon.scale`, `ib:operator:cmd`, `client_id` mutation. Non-negotiable. |
| **D-RS-F-i** | Cockpit UI form-factor v2 | (1) **Non-modal overlay** (already fixed post-RS-E via `RightInspectorShell`)<br>(2) **Dock mode** (main content shifts by 400 px)<br>(3) User toggle in Settings tab (default overlay) | **(3) User toggle** — Docker mode for deep multi-page workflow; overlay stays default |
| **D-RS-F-j** | Tracing sink | (1) Console (stdout)<br>(2) OTLP → Prometheus/Grafana (needs endpoint config)<br>(3) OpenAI hosted trace dashboard (needs `OPENAI_API_KEY`)<br>(4) Local JSONL file rotation | **(1) + (4)** — stdout for dev, JSONL rotation for prod; OTLP behind env flag `RESEARCH_COPILOT_OTLP_ENDPOINT` |

---

# Wave RS-F1 · SDK Migration Foundation

**Goal**: Replace `orchestrator.py` with a single `openai-agents` Agent bound to the existing MCP SSE server. Zero behavior change from FE perspective — SSE contract preserved. This is the risk-carrying phase; F2–F7 are additive on top.

**Duration**: 4–5 days · 1 agent (strict serial)

## Phase F1.1 · Dependencies & SDK Sanity

- **Repo**: `bifrost-research`
- **Depends on**: —
- **Sign-off**: required (locks dependency version for entire RS-F)

### Goal
Add `openai-agents` optional dep; bump `mcp` to a version compatible with both current server and SDK; verify a hello-world Agent connects to our MCP `:8796/sse` and can list all 28 tools.

### Files
- **modify**:
  - `pyproject.toml` — bump `mcp>=1.0.0,<2` → `mcp>=1.19.0,<2`; add `openai-agents>=0.6,<1` under `[project.optional-dependencies].copilot`
  - `Makefile` — new target `smoke-agents-sdk` — spins up MCP server locally + runs `scripts/smoke_agents_sdk.py`
- **new**:
  - `scripts/smoke_agents_sdk.py` — 40-line hello-world: import `Agent`, `Runner`, `MCPServerSse` → list tools → print count
  - `tests/copilot/test_sdk_bootstrap.py` — assert `openai_agents` import + MCP server has ≥ 28 tools discovered by SDK

### Verify
```bash
cd bifrost-research
pip install -e ".[dev,copilot]"
make test           # existing 285 tests must still pass
python scripts/smoke_agents_sdk.py   # prints "MCP tools discovered: 28"
```

### Acceptance
- `pip install -e ".[copilot]"` cleanly resolves (`openai-agents`, upgraded `mcp`, `openai`, `anthropic` co-exist)
- Smoke script reports all 28 tools including 4 write tools
- No regression in existing 285 tests
- CI stays green

---

## Phase F1.2 · Rewrite orchestrator on `Agent` + `MCPServerSse`

- **Repo**: `bifrost-research`
- **Depends on**: F1.1
- **Sign-off**: required (behavior parity gate)

### Goal
Replace `orchestrator.orchestrate()` internals with an `Agent` + `Runner.run_streamed`. Keep the public function signature and SSE frames identical (`token` / `tool_call` / `tool_result` / `error` / `done`). This means `POST /research/copilot/stream` FE contract is untouched.

### Files
- **new**:
  - `src/bifrost_research/copilot/agent_runtime.py` — new module owning the SDK Agent construction:
    - `build_agent(model_id: str) -> Agent` — single Agent, instructions from a shared const, `mcp_servers=[MCPServerSse]`, `mcp_config={"convert_schemas_to_strict": True, "include_server_in_tool_names": False}`
    - `stream_agent(messages, model_id, session_id) -> AsyncIterator[str]` — translates SDK stream events back to our SSE frames
  - `src/bifrost_research/copilot/write_gate.py` — small helper that forces `dry_run=True` on any write tool arg dict (moved from orchestrator; reused verbatim)
- **modify**:
  - `src/bifrost_research/copilot/orchestrator.py` — becomes a thin adapter:
    - `orchestrate()` delegates to `agent_runtime.stream_agent()`
    - `execute_approved_write()` stays unchanged (HMAC path already correct)
    - Old provider-loop code moves to `orchestrator_legacy.py` (kept for one wave in case rollback needed; deleted in F1.4 sign-off)
  - `src/bifrost_research/api/copilot.py` — no signature change; internal call target only
- **new**:
  - `src/bifrost_research/copilot/orchestrator_legacy.py` — copy of old orchestrator (deleted end of F1)

### Behavior
- SDK Agent uses `instructions=_SYSTEM` — same D10 system prompt
- SDK internal turns are opaque; the adapter yields:
  - `token` — from streamed `RawResponsesStreamEvent` (text deltas)
  - `tool_call` — from `RunItemStreamEvent(tool_call_item)`
  - `tool_result` — from `RunItemStreamEvent(tool_call_output_item)`
  - `done` — from final `MessageOutputItem` or run completion
- Write tools: intercept `tool_call_item` before invocation, force `dry_run=True`, strip `approval_token`
- Cost accounting: read `RunResult.raw_responses[*].usage` after run; feed into existing `rate_limit.record_usage`

### API contract changes
- **None** — `/research/copilot/stream` schema preserved

### Verify
```bash
cd bifrost-research
make lint && make test
# Contract test
pytest tests/api/test_copilot.py -v
# Live smoke (requires ANTHROPIC_API_KEY or OPENAI_API_KEY)
python scripts/smoke_copilot_chat.py  # (new small script that runs one turn end-to-end)
```

### Acceptance
- Existing `tests/api/test_copilot.py` passes without modification (contract preserved)
- Cost cap enforcement still triggers via `rate_limit.record_usage`
- Write-tool dry-run still forced in chat path (verified by `tests/mcp/test_write_tools.py`)
- `HypothesisCard`, `DiffApprovalCard`, `CopilotToolCallCard` on FE still render correctly (no FE changes needed)

---

## Phase F1.3 · Guardrails skeleton (D10 hard reject)

- **Repo**: `bifrost-research`
- **Depends on**: F1.2
- **Sign-off**: **required (safety gate)**

### Goal
Wire an SDK **InputGuardrail** and **OutputGuardrail** that reject any user prompt or model output containing D10-forbidden patterns. Rejection ends the turn with a canonical `error` SSE frame carrying `code: "D10_FREEZE"`.

### Files
- **new**:
  - `src/bifrost_research/copilot/guardrails.py`
    - `D10ForbiddenPatterns` — Pydantic model + compiled regex list:
      - `\bplace_order\b`, `\bib:operator:cmd\b`, `\bdaemon\.(start|stop|scale|resume)\b`, `\bclient_id\s*=\s*\d+\b`, `\bkubectl\s+(apply|delete|scale)\b`, `\bmake\s+(promote|deploy)`
    - `check_input(text: str) -> GuardrailResult` — returns tripwire if any pattern matches
    - `check_output(agent_output: str) -> GuardrailResult`
  - `tests/copilot/test_guardrails.py` — hits every pattern
- **modify**:
  - `src/bifrost_research/copilot/agent_runtime.py` — attach both guardrails to `Agent(guardrails=[...])`; on tripwire, emit `error` SSE and terminate

### Behavior
- Trigger example: user asks "Please stop the daemon" → InputGuardrail fires → SSE `error {code: "D10_FREEZE", message: "This action is blocked by D10 execution freeze policy."}`
- Guardrails run **before** MCP tools are considered — cheap deterministic reject

### API contract changes
- Extend `error` SSE with optional `code` field. Existing FE ignores unknown fields.

### Verify
```bash
pytest tests/copilot/test_guardrails.py -v
pytest tests/api/test_copilot.py -v
```

### Acceptance
- Every regex in `D10_FORBIDDEN_PATTERNS` produces a tripwire test
- No regression in copilot API tests
- Audit log `research.ai_action_log` records rejection reason (optional; can defer to F4)

---

## Phase F1.4 · Cleanup + Docs + Sign-off

- **Repo**: `bifrost-research`
- **Depends on**: F1.3
- **Sign-off**: required (F1 exit gate)

### Goal
Delete `orchestrator_legacy.py`, bump version, update `COCKPIT_RUNBOOK.md` and `RESEARCH_UX_DECISIONS.md`.

### Files
- **delete**:
  - `src/bifrost_research/copilot/orchestrator_legacy.py`
- **modify**:
  - `pyproject.toml` — `version = "0.17.0"` (RS-F opens minor version series)
  - `src/bifrost_research/__init__.py` — `__version__ = "0.17.0"`
  - `tests/test_package.py` — version assertion sync
  - `docs/COCKPIT_RUNBOOK.md` — new "SDK Runtime" section explaining Agent construction
  - `docs/RESEARCH_UX_DECISIONS.md` — append D-RS-F-a/b/c/d rows (F1 scope)
  - `docs/CAPABILITY_MATRIX.md` — add "RS-F1 Copilot SDK migration" row

### Verify
```bash
make lint && make test
grep -r "orchestrator_legacy" src/  # must be empty
```

### Acceptance
- Legacy module removed
- Version bumped, CAPABILITY_MATRIX updated
- Owner signs off before F2 starts

---

# Wave RS-F2 · DeepSeek Provider

**Goal**: Add `deepseek-chat` and `deepseek-reasoner` as first-class model options in Copilot. DeepSeek is called via its OpenAI-compatible endpoint (`https://api.deepseek.com/v1`), so we lean on the SDK's OpenAI-compatible layer — no custom `LlmProvider` class needed.

**Duration**: 2–3 days · 1 agent (strict serial; small)

## Phase F2.1 · DeepSeek model wiring

- **Repo**: `bifrost-research`
- **Depends on**: F1 (F1.4 signed off)
- **Sign-off**: not required

### Files
- **new**:
  - `src/bifrost_research/copilot/models.py` — `resolve_model_for_agent(model_id: str)` returns `openai_agents.OpenAIChatCompletionsModel(model=..., openai_client=AsyncOpenAI(base_url=..., api_key=...))`
    - `deepseek-chat` / `deepseek-reasoner` → base `https://api.deepseek.com/v1`, key `DEEPSEEK_API_KEY`
    - `gpt-*` → default OpenAI (Responses model when available; Chat Completions otherwise)
    - `claude-*` → SDK Anthropic adapter (via `openai-agents` third-party adapter if available; otherwise keep existing `ClaudeProvider` fallback for F2, migrate in F3)
    - `ollama:*` → base `http://127.0.0.1:11434/v1` (OpenAI-compatible)
  - `tests/copilot/test_deepseek_model.py` — mock the OpenAI-compatible endpoint (respx / httpx-mock); assert base URL and headers
- **modify**:
  - `src/bifrost_research/copilot/agent_runtime.py` — call `resolve_model_for_agent(model_id)` when building Agent
  - `src/bifrost_research/copilot/providers.py` — extend cost table `_PRICE_PER_MTOK["deepseek"] = (0.14, 0.28)` (public Nov-2026 pricing per 1M tokens)
  - `docs/COCKPIT_RUNBOOK.md` — new "DeepSeek" section: `DEEPSEEK_API_KEY` env, default endpoint, model list

### Env
| Env | Default | Purpose |
|-----|---------|---------|
| `DEEPSEEK_API_KEY` | — | Required for DeepSeek |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | Override for enterprise proxies |

### Verify
```bash
pytest tests/copilot/test_deepseek_model.py -v
# Live smoke (needs real key)
DEEPSEEK_API_KEY=... python scripts/smoke_copilot_chat.py --model deepseek-chat
```

### Acceptance
- `resolve_model_for_agent("deepseek-chat")` returns an SDK `Model` with base URL `api.deepseek.com/v1`
- Missing `DEEPSEEK_API_KEY` yields a clear `error` SSE frame (not a Python traceback)
- Cost accounting logs DeepSeek pricing tier

---

## Phase F2.2 · FE model catalog + Settings UX

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: F2.1
- **Sign-off**: not required

### Files
- **modify**:
  - `src/lib/cockpit/modelCatalog.ts`:
    - Extend `CopilotModelId` union with `'deepseek-chat' | 'deepseek-reasoner'`
    - Add rows to `COPILOT_MODELS` with `provider: 'deepseek'`
    - Change `DEFAULT_COPILOT_MODEL` to `'deepseek-chat'` (per D-RS-F-c)
  - `src/components/cockpit/SettingsTab.tsx` — group models by provider header; show a small "$/M in / $/M out" hint per model

### Verify
```bash
cd bifrost-trade-frontend
npm run lint && npm run build && npm run check:legacy-css
```

### Acceptance
- Cockpit → Settings shows DeepSeek Chat + Reasoner in the model list
- Selecting DeepSeek persists to `localStorage`
- Sending a message with `deepseek-chat` selected → BE receives correct `model` field → hits DeepSeek endpoint

---

## Phase F2.3 · E2E smoke

- **Repo**: `bifrost-research` + `bifrost-trade-frontend`
- **Depends on**: F2.2
- **Sign-off**: required (F2 exit gate)

### Verify (manual + scripted)
```bash
# BE
DEEPSEEK_API_KEY=... bdev restart research-api
curl -s http://127.0.0.1:8795/health   # version 0.17.x
# FE
bdev restart trade-ui
# Open http://127.0.0.1:5173/research → ⌘K → Settings → select "DeepSeek Chat"
# Send: "Summarize NVDA VRP over the last 5 trading days"
# Expect: tool_call: research.vrp.get_history → tool_result → coherent DeepSeek answer
```

### Acceptance
- DeepSeek returns valid answer using MCP tools (VRP / vol surface / SEPA reads)
- Cost is captured; `AiUsageTile` shows non-zero USD after 1–2 turns
- No D10 violation logs
- Owner signs off before F3

---

# Wave RS-F3 · Multi-Agent Topology (Handoffs + Agents-as-Tool)

**Goal**: Split the single Copilot Agent into a **Triage Agent** + 5 specialist Agents. Triage picks whether to handoff, or (for compose questions) hands to Verdict which uses specialists as `Agent.as_tool()`.

**Duration**: 5–7 days · 1 agent (serial for F3.1; then F3.2/F3.3 parallel)

## Agent graph

```
User → TriageAgent (small prompt, deterministic router)
        │
        ├── handoff → DiscoveryAgent  (SEPA · Event Radar · Momentum · Watchlist)
        ├── handoff → AnalyzeAgent    (VRP · Vol Surface · OpEx · GEX · Flow)
        ├── handoff → ValidateAgent   (Backtest · Regime stats · Walk-forward)
        ├── handoff → WriteAgent      (create/patch hypothesis · run backtest · dry_run enforced)
        ├── handoff → ExplainAgent    (docs / concepts / links to Runbook)
        └── handoff → VerdictAgent    (Morning brief · EOD verdict · Compose)
                        │
                        ├── uses Agent.as_tool() → DiscoveryAgent
                        ├── uses Agent.as_tool() → AnalyzeAgent
                        └── uses Agent.as_tool() → ValidateAgent
```

Every specialist Agent has:
- **Narrow instruction set** (200–300 tokens vs single 1500-token super-prompt)
- **Filtered MCP tool subset** (via SDK `tool_filter`)
- **Same D10 InputGuardrail + OutputGuardrail** (inherited)

## Phase F3.1 · Specialist Agents + Triage

- **Repo**: `bifrost-research`
- **Depends on**: F2.3
- **Sign-off**: required (locks agent graph for FE integration)

### Files
- **new**:
  - `src/bifrost_research/copilot/agents/graph.py`
    - `build_triage_agent()` — router with 6 `handoffs=[...]`
    - `build_discovery_agent()` — filtered to `research.discovery.*` MCP tools
    - `build_analyze_agent()` — filtered to `research.vrp.*`, `research.vol_surface.*`, `research.opex_cycle.*`
    - `build_validate_agent()` — filtered to `research.backtest.*`
    - `build_write_agent()` — filtered to `research.hypothesis.*` + `research.backtest.run_event_query` (all `dry_run=true` forced)
    - `build_explain_agent()` — no MCP tools; uses shared `RESEARCH_GLOSSARY` as tool
    - `build_verdict_agent()` — no direct MCP tools; imports Discovery/Analyze/Validate as `Agent.as_tool()`
  - `src/bifrost_research/copilot/agents/instructions/*.md` — one instruction file per specialist (versioned prompts, easier diffs)
- **modify**:
  - `src/bifrost_research/copilot/agent_runtime.py` — Runner starts from `build_triage_agent()`; SSE stream translates `RunItemStreamEvent(handoff_call_item)` → new `agent_handoff` frame

### SSE contract extension (backward compatible)
```json
{ "event": "agent_handoff", "from": "triage", "to": "analyze", "reason": "…", "session_id": "..." }
```
Existing FE ignores unknown events; F5 renders a chip.

### Verify
```bash
pytest tests/copilot/test_agent_graph.py -v
```
Cases:
- "Latest VRP for NVDA" → handoff Triage → Analyze
- "Should I hedge SPY today?" → handoff Triage → Verdict → (Discovery + Analyze as tools)
- "Create hypothesis: NVDA earnings vol crush" → handoff Triage → Write, dry_run enforced
- "What is a vanna?" → handoff Triage → Explain

### Acceptance
- All 6 specialists constructed with correct tool filters
- Handoffs work in unit tests using SDK's test runner (`Runner.run` with mock model)
- Guardrails inherited on every specialist

---

## Phase F3.2 · Verdict compose (Agent.as_tool)

- **Repo**: `bifrost-research`
- **Depends on**: F3.1
- **Can run in parallel with**: F3.3
- **Sign-off**: not required

### Goal
Verdict Agent explicitly uses Discovery / Analyze / Validate via `Agent.as_tool()` — the compose pattern where the manager stays in charge of the final answer, calling specialists in parallel for a bounded subtask.

### Files
- **modify**:
  - `src/bifrost_research/copilot/agents/graph.py` — Verdict Agent gets `tools=[discovery.as_tool(...), analyze.as_tool(...), validate.as_tool(...)]`
  - `src/bifrost_research/copilot/agents/instructions/verdict.md` — explicit "call all three specialists as tools, then synthesize"

### Verify
```bash
pytest tests/copilot/test_verdict_compose.py -v
```

### Acceptance
- "Morning verdict for NVDA" causes exactly 3 as-tool calls to specialists then a synthesis message

---

## Phase F3.3 · FE handoff chip + trace preview

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: F3.1
- **Can run in parallel with**: F3.2
- **Sign-off**: not required

### Files
- **new**:
  - `src/components/cockpit/AgentChip.tsx` — pill showing active agent ("Analyze", "Verdict", …) with a subtle color per specialist
  - `src/hooks/useCopilotAgentTrail.ts` — accumulates handoff frames from the SSE stream
- **modify**:
  - `src/api/aiCopilot.ts` — parse `agent_handoff` event and push to session state
  - `src/hooks/useCopilotSession.ts` — new state field `agentTrail: Array<{ from, to, at }>`
  - `src/components/cockpit/CopilotMessageList.tsx` — render `AgentChip` inline where a handoff occurred

### Verify
```bash
cd bifrost-trade-frontend && npm run lint && npm run build && npm run check:legacy-css
```

### Acceptance
- Every handoff visible as a chip in the chat stream
- Trail state clears on new session
- No visual regression in existing turns (single-agent path still looks identical)

---

# Wave RS-F4 · Guardrails + Sessions Persistence

**Goal**: Harden guardrails (already skeleton in F1.3) + persist Copilot chat history to `research.copilot_session` so users can resume conversations.

**Duration**: 3–4 days

## Phase F4.1 · Output guardrail + audit trail

- **Repo**: `bifrost-research`
- **Depends on**: F3.3
- **Sign-off**: not required

### Files
- **modify**:
  - `src/bifrost_research/copilot/guardrails.py` — strengthen `OutputGuardrail`:
    - Regex list (D10) same as input
    - Additional structural check: any model output claiming a live trade recommendation → tripwire
    - Passing tripwire records to `research.ai_action_log` with `action_kind='guardrail_reject'`
  - `src/bifrost_research/repositories/ai_action_log.py` — new helper `log_guardrail_rejection(reason, session_id, model)`

### Verify
```bash
pytest tests/copilot/test_guardrail_audit.py -v
```

### Acceptance
- Every guardrail trip writes exactly one `ai_action_log` row
- Tripped session terminates cleanly with `error {code: "D10_FREEZE"}`

---

## Phase F4.2 · `research.copilot_session` DDL + repository

- **Repo**: `bifrost-research`
- **Depends on**: F3.3
- **Can run in parallel with**: F4.1
- **Sign-off**: required (schema change)

### Files
- **modify**:
  - `src/bifrost_research/schema/schemas.py` — add `TABLE_RESEARCH_COPILOT_SESSION = 'research.copilot_session'`
  - `src/bifrost_research/schema/ddl.py` — new `_apply_copilot_session_ddl`:
    ```sql
    CREATE TABLE IF NOT EXISTS research.copilot_session (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      owner_id      text NOT NULL DEFAULT 'owner',
      title         text,
      model         text NOT NULL,
      agent_trail   jsonb DEFAULT '[]'::jsonb,
      messages      jsonb NOT NULL DEFAULT '[]'::jsonb,
      hypothesis_id uuid REFERENCES research.hypothesis(id) ON DELETE SET NULL,
      created_at    timestamptz NOT NULL DEFAULT now(),
      updated_at    timestamptz NOT NULL DEFAULT now(),
      expires_at    timestamptz NOT NULL DEFAULT (now() + interval '30 days'),
      status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived','expired'))
    );
    CREATE INDEX IF NOT EXISTS idx_copilot_session_owner ON research.copilot_session(owner_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_copilot_session_hyp ON research.copilot_session(hypothesis_id);
    ```
- **new**:
  - `src/bifrost_research/repositories/copilot_session.py` — `create / append_message / get / list_recent / archive` (async-safe)
  - `tests/schema/test_copilot_session_ddl.py`
  - `tests/repositories/test_copilot_session.py`

### Verify
```bash
make db-init-research
pytest tests/schema/test_copilot_session_ddl.py -v
pytest tests/repositories/test_copilot_session.py -v
```

### Acceptance
- Table created and indexed
- Owner reviews DDL diff and signs off

---

## Phase F4.3 · API + orchestrator plumbing for sessions

- **Repo**: `bifrost-research`
- **Depends on**: F4.2
- **Sign-off**: not required

### Files
- **modify**:
  - `src/bifrost_research/api/copilot.py` — extend request body: `session_id?: uuid`, `resume?: bool`; extend response with `session_id`
  - `src/bifrost_research/copilot/agent_runtime.py` — on every finished turn, upsert session (title auto-derived from first user message ≤ 60 chars)
- **new**:
  - `src/bifrost_research/api/copilot_sessions.py` — routes:
    - `GET /research/copilot/sessions?limit=20` — list recent
    - `GET /research/copilot/sessions/{id}` — full history
    - `DELETE /research/copilot/sessions/{id}` — archive
  - `tests/api/test_copilot_sessions.py`

### API contract additions
```
GET  /research/copilot/sessions           -> { rows: SessionSummary[] }
GET  /research/copilot/sessions/{id}      -> { session, messages }
DELETE /research/copilot/sessions/{id}    -> { archived: true }
```

### Acceptance
- Sending a message with no `session_id` → new session row appears
- Sending with `session_id` → messages appended
- List endpoint returns most recent first, capped at limit

---

# Wave RS-F5 · Cockpit v2 UX (Dock · Trace · Sessions)

**Goal**: Front-end catches up to the new capabilities — dock/overlay toggle, trace panel per turn, session list sidebar in Copilot tab.

**Duration**: 5–7 days

## Phase F5.1 · Dock vs Overlay toggle

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: F4.3
- **Sign-off**: not required

### Files
- **modify**:
  - `src/hooks/useCockpitDrawer.ts` — add `mode: 'overlay' | 'dock'`; persist to `localStorage`
  - `src/components/cockpit/CockpitDrawer.tsx` — render mode-dependent shell:
    - `overlay` (default) — current `RightInspectorShell` behavior (non-modal, page interactive)
    - `dock` — main content wraps with `pr-[400px]` when Cockpit open; page shifts left
  - `src/components/cockpit/SettingsTab.tsx` — new section "Display" with radio `Overlay | Dock`

### Acceptance
- Toggle instantly switches modes without state loss
- Dock mode: main content resizes; charts respect new width via ResizeObserver

---

## Phase F5.2 · Trace panel

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: F4.3
- **Can run in parallel with**: F5.1, F5.3, F5.4
- **Sign-off**: not required

### Files
- **new**:
  - `src/components/cockpit/CopilotTracePanel.tsx` — collapsible timeline: each row = one event (`token` / `tool_call` / `handoff` / `guardrail` / `done`) with duration
  - `src/hooks/useCopilotTrace.ts` — accumulates timestamped events per turn
- **modify**:
  - `src/components/cockpit/CopilotTab.tsx` — toolbar button "Show trace" toggles panel below composer

### Acceptance
- Every turn: 1 collapsible row per event, ms latency shown
- Long tool calls (> 2s) highlighted
- Panel persists collapsed state across sessions

---

## Phase F5.3 · Session list sidebar

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: F4.3
- **Can run in parallel with**: F5.1, F5.2, F5.4
- **Sign-off**: not required

### Files
- **new**:
  - `src/api/researchCopilotSessions.ts` — thin fetch wrappers
  - `src/hooks/useCopilotSessions.ts` — TanStack Query
  - `src/components/cockpit/SessionListSidebar.tsx` — compact list at Copilot tab top (last 10 sessions; click to load)
- **modify**:
  - `src/components/cockpit/CopilotTab.tsx` — mount SessionListSidebar above CopilotMessageList

### Acceptance
- Clicking a session loads its full message history
- New session button clears state
- Archive from row menu

---

## Phase F5.4 · Model catalog cleanup + docs

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: F4.3
- **Can run in parallel with**: F5.1, F5.2, F5.3
- **Sign-off**: required (F5 exit gate)

### Files
- **modify**:
  - `src/lib/cockpit/modelCatalog.ts` — final list: DeepSeek Chat, DeepSeek Reasoner, Claude 4.5 Sonnet, GPT-5, Ollama Llama 3.2; each with `cost_per_mtok_in/out`
  - `docs/CAPABILITY_MATRIX.md` — Wave RS-F row with all sub-phases + version `0.17.x`
  - `bifrost-research/docs/COCKPIT_RUNBOOK.md` — new "Wave RS-F Runtime" section (SDK, DeepSeek, agent graph, guardrails, sessions)
  - `bifrost-research/docs/RESEARCH_UX_DECISIONS.md` — D-RS-F-a … D-RS-F-j all marked `implemented`

### Acceptance
- Documentation matches shipped behavior; Owner reviews before considering RS-F closed

---

# Wave RS-F6 · Preset Agents Migration (optional / recommended)

**Goal**: Migrate Morning Prep and EOD Review agents to SDK Agents so they benefit from Sessions/Tracing/Guardrails.

**Duration**: 3 days · optional if F1–F5 already meet Owner needs

## Phase F6.1 · Morning + EOD as SDK Agents

- **Repo**: `bifrost-research`
- **Depends on**: F5.4
- **Sign-off**: not required

### Files
- **modify**:
  - `src/bifrost_research/copilot/agents/morning_prep.py` — use `Runner.run(build_verdict_agent(), ...)`; keep draft persistence semantics
  - `src/bifrost_research/copilot/agents/eod_review.py` — same pattern
  - `tests/agents/test_morning_prep.py`, `tests/agents/test_eod_review.py` — update mocks

### Acceptance
- CronJob (`k8s/agents/cronjob-morning-prep.yaml` / `cronjob-eod-review.yaml`) runs green
- Drafts written to `research.ai_draft` unchanged in shape
- Traces recorded

---

## Phase F6.2 · Cost-cap and dry-run integration

- **Repo**: `bifrost-research`
- **Depends on**: F6.1
- **Sign-off**: not required

### Files
- **modify**:
  - `src/bifrost_research/copilot/rate_limit.py` — accept agent name in the log line
  - CronJob env: `BIFROST_MORNING_AGENT_DRY_RUN=1` for staging soak

### Acceptance
- Morning/EOD respect daily cap without affecting user chat cap
- Metrics distinguishable via agent name in audit trail

---

# Wave RS-F7 · SandboxAgent for backtest iteration (deferred)

**Goal**: LLM proposes backtest parameters; SandboxAgent runs isolated iterations without polluting main DB; successful runs persist.

**Duration**: 1 week · **not committed** — decide after F1–F5 sit for a week.

---

# Global QA & Sign-off (RS-F closing gate)

Before declaring **Wave RS-F Complete**:

| Check | Location |
|-------|----------|
| `make lint && make test` (bifrost-research) — 285+ tests pass | `bifrost-research/` |
| `npm run lint && npm run build && npm run check:legacy-css` — legacy baselines unchanged | `bifrost-trade-frontend/` |
| `curl http://127.0.0.1:8795/health` → `version: 0.17.x` | Local `bdev` |
| Copilot chat with `deepseek-chat` returns coherent answer using ≥ 1 MCP tool | Manual UI |
| D10 pattern (e.g. "stop the daemon") → `error {code: "D10_FREEZE"}` | Manual UI |
| Session persistence: send 3 messages, refresh page, resume from Sessions list | Manual UI |
| Trace panel shows event timeline for every turn | Manual UI |
| CronJobs `morning-prep-smoke` + `eod-review-smoke` still Complete on K3s | `kubectl -n research create job … --from=cronjob/…` |
| No guardrail bypass via odd Unicode / whitespace | `tests/copilot/test_guardrail_bypass.py` (add if needed) |

---

# Risks & Rollback

| Risk | Mitigation | Rollback |
|------|-----------|----------|
| SDK ABI break during F1 | Pin `openai-agents` version; keep `orchestrator_legacy.py` until F1.4 signed | Revert `orchestrator.py` to legacy adapter |
| DeepSeek endpoint downtime | Model catalog falls back to Claude/GPT per user preference | User picks different model in Settings |
| MCP `mcp>=1.19` upgrade breaks existing `FastMCP` server | Bump in F1.1 blocked by smoke test; can pin `mcp>=1.19,<2` if v2 issues appear | Pin lower bound |
| Handoff explosion (agent bounces indefinitely) | SDK has built-in max_turns; guardrail also caps | Cap `max_tools`, `max_turns` at 12 |
| Guardrail false-positive on legitimate research question | Whitelist known safe phrases (`"place_order in test"` as literal quote allowed) | Add exception list; keep hard-reject default |
| Session table growth | 30-day TTL + `CREATE INDEX`; archive endpoint | Manual cleanup SQL |
| Cost surprises with DeepSeek Reasoner | Daily cap enforced pre-request | Reduce `COPILOT_DAILY_CAP_USD` |

---

# Timeline (calendar, small team of 2)

| Week | Wave/Phases | Deliverable |
|------|-------------|-------------|
| 1 | F1.1 → F1.4 | SDK migration complete, behavior parity |
| 2 (a) | F2.1 → F2.3 | DeepSeek live in Cockpit |
| 2 (b) | F3.1 | Specialist agents drafted |
| 3 | F3.2, F3.3 | Compose + FE handoff chip |
| 3 (b) | F4.1, F4.2, F4.3 | Guardrail + Sessions |
| 4 | F5.1–F5.4 | Cockpit v2 UX ships |
| 4 (b, optional) | F6.1, F6.2 | Preset agents on SDK |
| Later (deferred) | F7 | SandboxAgent |

---

# Post-RS-F Follow-ups (not in scope)

- **RS-G-optional**: Migrate MCP transport from SSE → Streamable HTTP once SDK Streamable HTTP is battle-tested internally.
- **RS-H-optional**: Bridge Cockpit ↔ Hermes (bidirectional handoff between Research and Ops domains). Independent Owner decision — new chat.
- **Autonomous Agent**: still deferred (was RS-E5 → now RS-Z-optional).
