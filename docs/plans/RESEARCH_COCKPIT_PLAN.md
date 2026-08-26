# Research Cockpit Plan — Wave RS-E (Post Muscle Program)

**Status**: **Complete** (Wave RS-E1 + RS-E2 + RS-E3 + RS-E4 · 2026-08-25)
**Team size assumption**: 2 people
**AI cost policy**: Reuse the existing user-side Cursor / Claude subscription for the chat runtime; no new paid data or model subscription for E1–E4.
**Total duration estimate**: 4–5 weeks (E1 + E2 + E3 + E4)
**Cross-repo scope**: `bifrost-research` (MCP server · new DDL · agent orchestration) · `bifrost-trade-frontend` (Cockpit UI · AI chat) · optionally `bifrost-trade-infra` (nginx / port routing)
**Trade execution freeze (D10)**: BLOCKED — no phase touches live order placement. AI agents **must not** call daemon control, `ib:operator:cmd`, or `place_order`.

---

## Program Rationale

Wave RS-A/B/C shipped a workflow skeleton + three Vol Labs + event-driven backtest. The Research domain now has 22 pages, 6 first-class objects (hypothesis · backtest_run · vrp · surface_fit · vanna_charm · sepa_signal), and 25 API routes. **But the workflow glue is still missing**:

1. Users must remember the last symbol / date / hypothesis by hand when moving between pages.
2. Hypothesis is a record, not an orchestrator — nothing actively drives it.
3. AI (Claude / GPT) is used ad-hoc in the IDE, disconnected from Research data. There is no production surface for AI to consume Research context.
4. Morning routine and EOD review are still manual — no automation replaces the "flip through 6 pages before market open" habit.

**Wave RS-E** answers this with a persistent, cross-page **Research Cockpit** panel + AI Copilot layered on top:

- **RS-E1** — Static Cockpit shell (no AI). Pinboard, session context, quick actions, freshness lamps.
- **RS-E2** — Read-only AI Copilot. MCP server exposing all Research GET routes as tools; chat UI in Cockpit; every AI answer carries source links back to Lab pages.
- **RS-E3** — Preset Morning / EOD Agents. Scheduled draft generation to Cockpit; user approves items into `research.hypothesis` / `research.backtest_run`.
- **RS-E4** — Interactive AI write actions. Chat says "backtest this hypothesis" → AI drafts request → Cockpit shows diff card → user approves → API called + audit log written.

**Explicitly deferred**:

- **Autonomous Agent** (would be RS-E5) — 2-person team ROI too low; audit cost > time saved. Not building.
- **Wave RS-D** (Jupyter / MLflow / Dagster productionization) — was next in the muscle plan; Cockpit + AI covers ~80% of Jupyter's small-team value. Re-evaluate after E4.

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

- Wave RS-E1: E1.1 → E1.2 → E1.3 → E1.4 (strict serial — same shared shell)
- Wave RS-E2: E2.1 → (E2.2 · E2.3 parallel) → E2.4
- Wave RS-E3: E3.1 → (E3.2 · E3.3 parallel) → E3.4
- Wave RS-E4: E4.1 → (E4.2 · E4.3 parallel) → E4.5

---

## Owner Decisions (needed before RS-E1)

| ID | Question | Options | Recommendation |
|----|----------|---------|----------------|
| **D-RS-E-a** | Cockpit UI form-factor | (1) Right-side collapsible **Drawer** (`⌘K` opens, `Esc` closes) — overlays content on demand, doesn't shrink Lab pages<br>(2) Docked side-panel — always present, shrinks main content<br>(3) Floating modal — hurts multi-page workflow | **(1) Drawer** — 2-person team wants Lab pages full-width most of the time; Cockpit is on-demand |
| **D-RS-E-b** | State persistence | (1) `Zustand` + `localStorage` (per-browser)<br>(2) URL search params (shareable)<br>(3) Server-backed `research.cockpit_session` table (multi-device)<br>(4) All three: URL for shareable subset, localStorage for private prefs, server for hypothesis/pin state | **(4)** — pinned hypotheses **must** be server-backed (they are `research.hypothesis`); UI prefs local; symbol/date URL-syncable |
| **D-RS-E-c** | AI provider strategy | (1) Anthropic Claude only<br>(2) OpenAI only<br>(3) Multi-provider abstraction (LiteLLM / provider-agnostic)<br>(4) Local Ollama fallback | **(3) Multi-provider via abstraction** — pluggable; Owner can pick Claude 4.5 / GPT-5 / etc. from Cockpit settings; local Ollama optional dev fallback |
| **D-RS-E-d** | MCP server placement | (1) Embedded in `bifrost-research` API `:8795` as `/mcp` subroute<br>(2) Standalone process `research-mcp:8796` (new K8s deploy)<br>(3) New repo `bifrost-research-mcp`<br>(4) `platform-api` proxy layer | **(2) Standalone process** — MCP has a different lifecycle (long-lived SSE / stdio); mixing with FastAPI HTTP hurts hot-reload; keep the same repo (`bifrost-research/src/bifrost_research/mcp/`) for shared DB access |
| **D-RS-E-e** | AI-initiated write approval strictness | (1) All writes require explicit user approval (diff card + click)<br>(2) Small writes auto (e.g. add tag) + large writes gated<br>(3) All writes auto (Owner trusts AI) | **(1) All writes require approval** — audit cost is bounded, learning phase; revisit once trust accumulates |
| **D-RS-E-f** | Morning / EOD Agent scheduling | (1) K8s CronJob (fixed UTC time)<br>(2) On-demand from Cockpit ("Run Morning Prep now")<br>(3) User-configurable per-account cron<br>(4) Both (1) + (2) | **(4)** — CronJob for reliability (06:30 ET / 16:30 ET) + on-demand button for ad-hoc |
| **D-RS-E-g** | AI action audit log location | (1) New `research.ai_action_log` table<br>(2) Inline as JSON append to `research.hypothesis.notes` field<br>(3) K8s ConfigMap / logfile<br>(4) No audit (dev only) | **(1) New `research.ai_action_log` table** — durable, queryable, cross-hypothesis; required for E4 approval history |
| **D-RS-E-h** | MCP transport | (1) stdio (local dev only)<br>(2) HTTP + SSE (standard, deployable)<br>(3) WebSocket | **(2) HTTP + SSE** — matches Anthropic MCP standard, deployable to K3s, Cursor/Claude Desktop compatible |
| **D-RS-E-i** | Cost / rate-limit strategy | (1) Hard cap `MAX_TOKENS_PER_DAY` server-side<br>(2) Session-level rate-limit only<br>(3) None (trust Cursor's own limits) | **(1)** — expose in Cockpit "AI Usage" tile; default $2/day dev cap raising to $10/day prod |

---

# Wave RS-E1 · Static Cockpit (no AI)

**Goal**: Ship a cross-page, persistent Cockpit drawer that binds the existing 22 Research pages into a single workflow surface. Zero AI in this Wave. E2 layers AI on the same surface.

**Duration**: 5–7 days · 1 agent (serial phases; touches shared FE files)

## Phase RS-E1.1 · Cockpit Shell + Drawer

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: — (starting phase)
- **Can run in parallel with**: —
- **Sign-off**: required (locks Drawer surface for E1.2 / E1.3 / E1.4)

### Goal

Introduce a global right-side Drawer that opens with `⌘K` (or a header pin icon), closes with `Esc`. The Drawer is present on **every** Research page (`/research/*`). Empty tabs are fine at this Phase — later phases fill each tab.

### Files

- **new**:
  - `src/components/cockpit/CockpitDrawer.tsx` — root component (Radix `Sheet` variant)
  - `src/components/cockpit/CockpitTabs.tsx` — top-level tab bar: `Pins` · `Context` · `Actions` · `Copilot` (Copilot disabled + tooltip "E2") · `Settings`
  - `src/components/cockpit/index.ts` — barrel
  - `src/hooks/useCockpitDrawer.ts` — Zustand store: `{ open: bool, tab: TabId, openWithTab(t) }`
  - `src/lib/cockpit/keybinds.ts` — global `⌘K` / `Esc` keybinding
- **modify**:
  - `src/layout/AppShell.tsx` (or equivalent) — mount `<CockpitDrawer />` at the root, after `<Outlet />`
  - `src/layout/AppHeader.tsx` — add pin icon button that toggles the drawer
  - `bifrost-trade-frontend/docs/CAPABILITY_MATRIX.md` — new Wave RS-E row

### Behavior

- Drawer overlays right 400 px (mobile 100%), Radix `Sheet` `side="right"` variant
- Header uses shadcn Tabs; tab state persists to `localStorage` key `bifrost.cockpit.tab`
- `useCockpitDrawer.getState().openWithTab('actions')` — other pages can programmatically open specific tabs
- Focus trap active while open; body scroll locked
- Every Cockpit surface uses Dense UI tokens (`text-dense-body/label/meta`), no new module CSS

### Verify

```bash
cd bifrost-trade-frontend
npm run lint && npm run build && npm run check:legacy-css
```

### Acceptance

- Drawer opens/closes with `⌘K` / `Esc` / header pin
- Tab state survives page refresh
- Visible on all `/research/*` routes
- `check:legacy-css` baselines `HARDCODED_TYPO=29` / `RAW_PNL=37` **not raised**

---

## Phase RS-E1.2 · Pinboard + Persistent Store

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: RS-E1.1
- **Can run in parallel with**: —
- **Sign-off**: not required

### Goal

Cockpit tab `Pins` lists user-pinned Symbols · Hypotheses · Discovery hits. Pins survive refresh. Each pin has a "jump" action that opens the source Lab with context pre-filled.

### Files

- **new**:
  - `src/store/cockpitPinStore.ts` — Zustand store with `persist` middleware
    - state: `{ symbols: string[], hypothesisIds: string[], hits: DiscoveryHitRef[] }`
    - actions: `pinSymbol / unpinSymbol / pinHypothesis / unpinHypothesis / pinHit / unpinHit / clear`
  - `src/components/cockpit/PinsTab.tsx`
  - `src/components/cockpit/PinChip.tsx` — dense chip with lucide `Pin` icon + hover-remove
  - `src/hooks/useCockpitPins.ts` — thin wrapper hook
- **modify**:
  - `src/components/symbol/SymbolPicker.tsx` — pin icon next to selected symbol (calls `pinSymbol`)
  - `src/components/research/SaveAsHypothesisButton.tsx` — auto-pin hypothesis on save
  - `src/pages/research/ResearchHomePage.tsx` — auto-pin top-N SEPA hits when Cockpit opens

### Data types

```ts
export type DiscoveryHitRef = {
  kind: 'sepa' | 'event' | 'iv' | 'sentiment'
  symbol: string
  ts: string            // ISO
  detail: Record<string, unknown>
  originPage: string    // for jump link
}
```

### Behavior

- LocalStorage key: `bifrost.cockpit.pins.v1`
- Max 24 pins per kind (LRU eviction)
- Empty state uses `EmptyState` primitive with a CTA "Pin the current symbol"

### Verify

```bash
npm run lint && npm run build && npm run check:legacy-css
```

### Acceptance

- Pin a symbol from SymbolPicker → visible in Cockpit → refresh page → pin persists
- Pin a hypothesis from `SaveAsHypothesisButton` → shows in Cockpit Pins tab immediately
- Click a pinned hit → navigates to `originPage` with symbol pre-filled via `ResearchContextBar` URL sync

---

## Phase RS-E1.3 · Session Context Panel

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: RS-E1.1
- **Can run in parallel with**: RS-E1.2
- **Sign-off**: not required

### Goal

Cockpit tab `Context` shows current session state at a glance: symbol, trade date, regime tag, IV rank, current hypothesis focus. Two-way binding with `ResearchContextBar` — changes in Cockpit propagate to the current Lab page and vice versa.

### Files

- **new**:
  - `src/components/cockpit/ContextTab.tsx`
  - `src/components/cockpit/ContextSnapshot.tsx` — 4-row DL: Symbol / Trade Date / Regime / IV Rank
  - `src/hooks/useCockpitContext.ts` — merges `useResearchContext()` + latest VRP / regime data
- **modify**:
  - `src/components/research/ResearchContextBar.tsx` — subscribe to `cockpitContextStore` (already exists as `useResearchContext`); no functional change, just source-of-truth alignment

### Behavior

- If no symbol picked: friendly hint "Pick a symbol in any Lab page or Pin one below"
- IV Rank + regime call `useVrpLatest(symbol)` — cached, no new fetch pressure
- Freshness lamp: green if latest `stock_signal_vrp_daily` within 2 trading days, yellow within 5, red older

### Verify

```bash
npm run lint && npm run build && npm run check:legacy-css
```

### Acceptance

- Change symbol in Cockpit → all open Research pages see new symbol via `ResearchContextBar`
- Change symbol in a Lab page → Cockpit Context tab reflects immediately

---

## Phase RS-E1.4 · Quick Actions + Freshness Lamps

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: RS-E1.2, RS-E1.3
- **Can run in parallel with**: —
- **Sign-off**: required (locks the E1 static Cockpit contract for E2 AI layer)

### Goal

Cockpit tab `Actions` provides one-click research jumps that carry current context. Also shows a `Freshness` panel: three health lamps (Hypothesis / Backtest / Discovery data recency).

### Files

- **new**:
  - `src/components/cockpit/ActionsTab.tsx`
  - `src/components/cockpit/QuickActionButton.tsx` — icon + label + hint; uses `IconActionButton`
  - `src/components/cockpit/FreshnessLampGrid.tsx`
  - `src/hooks/useCockpitFreshness.ts` — polls 60s: last hypothesis create ts, last backtest_run ts, last SEPA hit ts

### Actions (v1)

| Icon | Label | Effect |
|------|-------|--------|
| `BookmarkPlus` | Save current view as Hypothesis | Opens `SaveAsHypothesisButton`'s Dialog pre-filled |
| `Beaker` | Run Event Query on current hypothesis | Navigate `/research/backtest?tab=event-query&hypothesis_id=xxx` |
| `LineChart` | Open Vol Surface | Navigate `/research/vol-surface-lab?symbol=xxx` |
| `Activity` | Open IV-RV Spread | Navigate `/research/vrp-lab?symbol=xxx` |
| `CalendarDays` | Open OpEx Cycle | Navigate `/research/opex-cycle-lab?symbol=xxx` |
| `Compass` | Back to Research Home | `/research` |

### Verify

```bash
npm run lint && npm run build && npm run check:legacy-css
```

### Acceptance

- All 6 Quick Actions navigate correctly with context pre-filled
- Three freshness lamps display live state; hover shows exact timestamp
- Full round-trip: open Cockpit → pin NVDA → "Save as Hypothesis" → "Run Event Query" → land on Backtest with hypothesis dropdown pre-selected
- Docs updated: `docs/CAPABILITY_MATRIX.md` Wave RS-E1 row complete

---

# Wave RS-E2 · Read-only AI Copilot

**Status**: ✅ COMPLETE (2026-08-25) — MCP `:8796` · Copilot SSE · FE Copilot/Settings · bifrost-research **0.14.0**

**Goal**: Layer a chat surface into Cockpit's `Copilot` tab, backed by a Research MCP server that exposes all read APIs as tools. AI can answer arbitrary questions about Research data, cite sources, and open Lab pages. **Zero write paths in this Wave.**

**Duration**: 8–10 days · 2 agents (E2.1 backend / E2.2 frontend can parallel after Owner decisions)

## Phase RS-E2.1 · Research MCP Server

- **Repo**: `bifrost-research`
- **Depends on**: Owner decisions D-RS-E-c/d/h/i finalized
- **Can run in parallel with**: —
- **Sign-off**: required (locks tool schemas for E2.2)

### Goal

New standalone MCP server on `:8796` exposing 25 Research API routes as MCP tools. Uses `mcp` Python SDK (`modelcontextprotocol/python-sdk`). Runs alongside `bifrost-research` API (same DB access, same repo).

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/mcp/__init__.py`
  - `bifrost-research/src/bifrost_research/mcp/server.py` — `FastMCP` app; tool registration
  - `bifrost-research/src/bifrost_research/mcp/tools/hypothesis.py`
  - `bifrost-research/src/bifrost_research/mcp/tools/backtest.py`
  - `bifrost-research/src/bifrost_research/mcp/tools/vrp.py`
  - `bifrost-research/src/bifrost_research/mcp/tools/vol_surface.py`
  - `bifrost-research/src/bifrost_research/mcp/tools/opex_cycle.py`
  - `bifrost-research/src/bifrost_research/mcp/tools/discovery.py` — daily brief synth · SEPA · event radar · momentum
  - `bifrost-research/scripts/run_mcp.py` — entrypoint (`python -m bifrost_research.mcp.server` also works)
  - `bifrost-research/tests/mcp/test_tools_registered.py`
  - `bifrost-research/k8s/mcp/deployment.yaml` — new Deployment + Service `:8796`
- **modify**:
  - `bifrost-research/pyproject.toml` — add `mcp>=1.0.0` dependency; bump to `0.14.0`
  - `bifrost-research/Makefile` — add `run-mcp`, `test-mcp` targets
  - `bifrost-research/src/bifrost_research/__init__.py` bump
  - `bifrost-research/tests/test_package.py` bump

### Tool naming convention

`research.<domain>.<action>` — e.g. `research.hypothesis.list_active`, `research.backtest.list_runs`, `research.vrp.get_latest`, `research.opex_cycle.get_current`.

Every tool has:

- `description`: 1-2 sentence natural-language summary + "**Read-only**. Does not modify data."
- `input_schema`: JSON Schema for LLM validation
- `output_schema`: envelope matches existing HTTP API `{ ok, data, error? }`
- All tools call into existing `repositories/*.py` — no logic duplication

### API contract

MCP endpoint on `:8796/sse` (SSE transport per D-RS-E-h). Discovery: `GET :8796/sse` returns MCP handshake per spec.

### Verify

```bash
cd bifrost-research
make lint
make test
make run-mcp &
# Handshake smoke:
curl -s -H 'Accept: text/event-stream' http://127.0.0.1:8796/sse | head -20
# Tool list smoke:
python -m bifrost_research.mcp.list_tools_smoke   # helper script that connects + lists tools
```

### Acceptance

- 25+ tools registered; `list_tools` handshake returns each with schema
- No tool touches write path (grep the mcp/ folder for `POST` / `PATCH` / `DELETE` / `INSERT` / `UPDATE` / `DELETE` → zero hits, except SELECTs)
- `test_tools_registered.py` asserts tool count and schema validity
- Deployment yaml applies cleanly to `research` namespace (dry-run tested)
- Cursor Desktop can be pointed at `http://127.0.0.1:8796/sse` and lists tools

---

## Phase RS-E2.2 · Cockpit Copilot Tab (FE Chat UI)

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: RS-E1.4 (Cockpit shell + tabs), RS-E2.1 (MCP contract stable)
- **Can run in parallel with**: RS-E2.3
- **Sign-off**: not required

### Goal

Populate Cockpit `Copilot` tab with a chat UI: message list · streaming response · input box · model selector dropdown. Backed by the AI provider abstraction (D-RS-E-c). AI can call MCP tools; tool calls display inline.

### Files

- **new**:
  - `src/components/cockpit/CopilotTab.tsx`
  - `src/components/cockpit/CopilotMessageList.tsx`
  - `src/components/cockpit/CopilotComposer.tsx`
  - `src/components/cockpit/CopilotToolCallCard.tsx` — collapsible; shows tool name + input + result
  - `src/components/cockpit/CopilotSourceLink.tsx` — clickable chip that navigates to source Lab page
  - `src/api/aiCopilot.ts` — SSE client wrapping the AI orchestrator endpoint
  - `src/hooks/useCopilotSession.ts` — Zustand: messages, streaming state, model selection
  - `src/lib/cockpit/modelCatalog.ts` — provider abstraction: Claude 4.5 / GPT-5 / (optional) local Ollama
- **modify**:
  - `src/components/cockpit/CockpitTabs.tsx` — Copilot tab enabled

### AI orchestrator

The FE talks to a small orchestrator (in `bifrost-research` on `:8795/research/copilot/stream`, added in E2.3) that:

1. Receives user prompt + conversation history
2. Calls the chosen LLM provider (via `litellm` or provider-agnostic wrapper)
3. When LLM emits a `tool_use`, dispatches to MCP `:8796`, appends result, resumes generation
4. Streams tokens back to FE via SSE

**FE does not talk to LLM API keys directly** — keys stay server-side (K8s Secret).

### Verify

```bash
npm run lint && npm run build && npm run check:legacy-css
```

### Acceptance

- Prompt "What are my active hypotheses about NVDA?" → chat shows tool call `research.hypothesis.list_active` + result + human answer with clickable source chips
- Streaming works (tokens appear character by character)
- Model dropdown switches provider mid-conversation
- No layout shift or dense-ui token violation

---

## Phase RS-E2.3 · Copilot Orchestrator Endpoint

- **Repo**: `bifrost-research`
- **Depends on**: RS-E2.1
- **Can run in parallel with**: RS-E2.2
- **Sign-off**: not required (contract locked in E2.1)

### Goal

Add `POST /research/copilot/stream` to `bifrost-research` API. Wraps LLM provider + MCP tool dispatch. Streams via SSE.

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/api/copilot.py`
  - `bifrost-research/src/bifrost_research/copilot/__init__.py`
  - `bifrost-research/src/bifrost_research/copilot/orchestrator.py` — main loop: LLM call → tool_use → MCP dispatch → LLM continue
  - `bifrost-research/src/bifrost_research/copilot/providers.py` — Claude / GPT / Ollama abstractions
  - `bifrost-research/src/bifrost_research/copilot/rate_limit.py` — per D-RS-E-i
  - `bifrost-research/tests/api/test_copilot.py`
- **modify**:
  - `bifrost-research/src/bifrost_research/api/app.py` — include router
  - `bifrost-research/pyproject.toml` — add `httpx-sse`, `anthropic>=0.30`, `openai>=1.40`; bump version if not already at 0.14

### API contract

```
POST /research/copilot/stream
Body: {
  messages: [{ role, content }],
  model: "claude-4.5-sonnet" | "gpt-5" | "ollama:...",
  max_tools: 8,
  session_id?: string
}
Response: text/event-stream
Events: token · tool_call · tool_result · error · done
```

### Verify

```bash
make test
uvicorn bifrost_research.api.app:app --port 8795 &
curl -N -X POST http://127.0.0.1:8795/research/copilot/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"List my active hypotheses"}],"model":"claude-4.5-sonnet"}'
```

### Acceptance

- `test_copilot.py` passes with mocked LLM provider
- Rate-limit hit returns HTTP 429 with `Retry-After`
- No write MCP tool can be dispatched here (Wave E2 is read-only — enforce a `read_only=True` flag in the orchestrator that filters out write tools)
- Audit line written to stdout per turn (`bifrost.copilot.audit`)

---

## Phase RS-E2.4 · Cost / Usage Tile

- **Repo**: `bifrost-trade-frontend` + minor `bifrost-research`
- **Depends on**: RS-E2.2, RS-E2.3
- **Can run in parallel with**: —
- **Sign-off**: not required

### Goal

Cockpit `Settings` tab shows AI usage: today's tokens / cost estimate / remaining cap; a "Clear session" button; provider status.

### Files

- **new**:
  - `src/components/cockpit/SettingsTab.tsx`
  - `src/components/cockpit/AiUsageTile.tsx`
  - `bifrost-research/src/bifrost_research/api/copilot.py` — add `GET /research/copilot/usage`
- **modify**: none

### Behavior

- Usage tile polls `/research/copilot/usage` every 30s
- Cap breach → Copilot tab shows red banner "Daily AI cap reached — resets at 00:00 UTC"
- Provider status shows last-known error (429 / 500) if any

### Verify

Standard trio (both repos).

### Acceptance

- E2 exit criteria (full Wave):
  - Cockpit Copilot answers 3 canned questions with tool calls + source citations
  - Zero write-path invocations (test asserts against mock)
  - Cost tile shows real numbers
  - Docs `CAPABILITY_MATRIX.md` E2 row complete
  - `bifrost-research` version bump published (e.g. **0.14.0**)

---

# Wave RS-E3 · Preset Morning / EOD Agents

**Goal**: Two scheduled agents produce structured drafts into Cockpit for user review. Nothing is written to `research.*` without explicit approve click.

**Duration**: 6–7 days

## Phase RS-E3.1 · Audit Log DDL + Draft Table

- **Repo**: `bifrost-research`
- **Depends on**: RS-E2.4
- **Can run in parallel with**: —
- **Sign-off**: required (locks contract for E3.2 / E3.3 / E3.4)

### Goal

Two new tables in `research` schema:

- `research.ai_action_log` — every AI-proposed action (whether approved or not)
- `research.ai_draft` — pending drafts awaiting user approval (Morning Brief / EOD Verdict / etc.)

### Data model

```sql
CREATE TABLE IF NOT EXISTS research.ai_action_log (
    id              text PRIMARY KEY,                    -- ULID
    session_id      text,
    action_kind     text NOT NULL,                       -- 'draft_hypothesis' | 'draft_verdict' | 'run_backtest' | 'chat_answer'
    action_source   text NOT NULL,                       -- 'morning_agent' | 'eod_agent' | 'user_chat'
    model           text,                                -- provider id (e.g. claude-4.5-sonnet)
    input           jsonb,
    output          jsonb,
    tool_calls      jsonb,
    status          text NOT NULL DEFAULT 'proposed',    -- proposed | approved | rejected | executed | error
    approved_by     text,                                -- user id / 'owner'
    approved_at     timestamptz,
    executed_at     timestamptz,
    executed_result jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_action_log_status ON research.ai_action_log(status) WHERE status = 'proposed';
CREATE INDEX IF NOT EXISTS ai_action_log_source ON research.ai_action_log(action_source, created_at DESC);

CREATE TABLE IF NOT EXISTS research.ai_draft (
    id              text PRIMARY KEY,
    kind            text NOT NULL,                       -- 'morning_brief' | 'eod_verdict' | 'hypothesis_suggestion'
    payload         jsonb NOT NULL,                      -- full draft body (markdown + structured fields)
    scope           text NOT NULL,                       -- 'global' | hypothesis_id | symbol
    status          text NOT NULL DEFAULT 'pending',     -- pending | approved | dismissed | expired
    generated_by    text NOT NULL,                       -- agent id
    linked_action_id text REFERENCES research.ai_action_log(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz
);
CREATE INDEX IF NOT EXISTS ai_draft_pending ON research.ai_draft(status, created_at DESC) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ai_draft_scope ON research.ai_draft(scope);
```

### Files

- **modify**:
  - `bifrost-research/src/bifrost_research/schema/ddl.py`
  - `bifrost-research/src/bifrost_research/schema/schemas.py`
  - `bifrost-research/tests/schema/test_ddl.py` — smoke both tables

### Verify

```bash
make lint && make test
make db-init-research   # applies new tables idempotently
```

### Acceptance

- Both tables idempotently created
- Wave RS-E3 row starts in `docs/CAPABILITY_MATRIX.md`

---

## Phase RS-E3.2 · Morning Prep Agent (CronJob + on-demand)

- **Repo**: `bifrost-research`
- **Depends on**: RS-E3.1
- **Can run in parallel with**: RS-E3.3
- **Sign-off**: not required

### Goal

Agent that runs at 06:30 UTC-5 (11:30 UTC) MON-FRI and produces one `ai_draft` per active hypothesis + one global "Today's Discoveries" draft.

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/copilot/agents/__init__.py`
  - `bifrost-research/src/bifrost_research/copilot/agents/morning_prep.py`
  - `bifrost-research/scripts/run_morning_agent.py` — entrypoint
  - `bifrost-research/k8s/agents/cronjob-morning-prep.yaml` — schedule `30 11 * * MON-FRI`
  - `bifrost-research/src/bifrost_research/api/agents.py` — `POST /research/agents/morning/run` (on-demand trigger, gated behind operator token)
  - `bifrost-research/tests/agents/test_morning_prep.py`
- **modify**:
  - `bifrost-research/src/bifrost_research/api/app.py` — include agents router

### Agent logic (per D-RS-E-f)

1. `SELECT * FROM research.hypothesis WHERE status='active'`
2. For each hypothesis: pull latest VRP / regime / event radar / GEX per symbol → build LLM context
3. LLM prompt: "Given <yesterday close> and <overnight events>, produce a 3-bullet status update for this hypothesis"
4. Insert row into `research.ai_draft` with `kind='eod_verdict'` — wait, this is morning. Use `kind='morning_brief'`.
5. Additionally, one global `kind='morning_brief'` with `scope='global'`: top-5 discoveries (SEPA / IV / event radar) not yet linked to any hypothesis
6. Every LLM turn logged to `research.ai_action_log` with `action_source='morning_agent'`

### Verify

```bash
make test
# Local dry-run:
BIFROST_MORNING_AGENT_DRY_RUN=1 python -m bifrost_research.scripts.run_morning_agent
```

### Acceptance

- Dry-run outputs drafts to stdout (no DB write)
- Real run inserts N drafts where N = active hypotheses + 1
- CronJob dry-apply passes; test cluster `kubectl create --dry-run=server` OK

---

## Phase RS-E3.3 · EOD Review Agent (CronJob + on-demand)

- **Repo**: `bifrost-research`
- **Depends on**: RS-E3.1
- **Can run in parallel with**: RS-E3.2
- **Sign-off**: not required

### Goal

Runs at 16:30 UTC-5 (21:30 UTC) MON-FRI. For each active hypothesis, drafts a "today's evidence → recommended verdict update" summary.

### Files

Mirrors E3.2 structure:

- **new**:
  - `bifrost-research/src/bifrost_research/copilot/agents/eod_review.py`
  - `bifrost-research/scripts/run_eod_agent.py`
  - `bifrost-research/k8s/agents/cronjob-eod-review.yaml` — schedule `30 21 * * MON-FRI`
  - `POST /research/agents/eod/run`
  - `bifrost-research/tests/agents/test_eod_review.py`

### Agent logic

1. For each `active` hypothesis:
   - Pull today's OHLCV / IV change / event radar hits / any backtest_run created today linked via `linked_backtest_ids`
   - LLM prompt: "Based on today's data, propose a status update. Options: keep active / promote to validated / demote to rejected. Provide 1-2 sentence rationale."
2. Insert `ai_draft` with `kind='eod_verdict'`, `scope=hypothesis_id`
3. Log turn to `research.ai_action_log`

### Verify

Same shape as E3.2.

### Acceptance

- Per hypothesis, one draft with a proposed status transition + rationale
- If no data changed today, agent produces a draft that says "no material change; keep active"

---

## Phase RS-E3.4 · Cockpit Approval Inbox

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: RS-E3.1 / E3.2 / E3.3
- **Can run in parallel with**: —
- **Sign-off**: required (Wave E3 sign-off gate)

### Goal

Cockpit gets a new `Inbox` tab (or reuses Pins → tab name `Inbox / Pins`) that lists pending drafts. Each draft shows an `Approve` / `Dismiss` action. Approve → API call that writes to canonical table + marks draft `approved`.

### Files

- **new**:
  - `src/components/cockpit/InboxTab.tsx`
  - `src/components/cockpit/DraftCard.tsx` — shows markdown + Approve / Dismiss
  - `src/api/researchDrafts.ts`
  - `src/hooks/useResearchDrafts.ts` — TanStack Query list + approve mutations
- **modify**:
  - `src/components/cockpit/CockpitTabs.tsx` — insert Inbox tab (with unread count badge)
  - `bifrost-research/src/bifrost_research/api/agents.py`:
    - `GET  /research/drafts?status=pending&kind=...`
    - `POST /research/drafts/{id}/approve`  — writes to `research.hypothesis` (status transition) + marks draft approved + inserts row into `ai_action_log` with `status='executed'`
    - `POST /research/drafts/{id}/dismiss` — marks draft dismissed

### Verify

Both repos' standard trio.

### Acceptance

- E3 exit (full Wave):
  - Trigger Morning Agent → inbox shows N drafts within 10s
  - Approve one → status flip visible on Home + Hypothesis page within 5s (TanStack invalidation)
  - Dismiss → hidden but recorded in `ai_action_log`
  - Nothing writes to `research.hypothesis` / `research.backtest_run` without a corresponding approved `ai_action_log` row

---

# Wave RS-E4 · Interactive AI Write Actions

**Goal**: Allow user to say "backtest this hypothesis with long straddle" in Copilot chat. AI drafts a write request, Cockpit shows a diff card, user approves, action executes. Same audit path as E3 drafts.

**Duration**: 5–6 days

## Phase RS-E4.1 · Write-mode MCP Tools

- **Repo**: `bifrost-research`
- **Depends on**: RS-E3.4
- **Can run in parallel with**: —
- **Sign-off**: required (locks the write surface)

### Goal

Add write tools to MCP server behind a `dry_run: bool` flag. Default `dry_run=true` returns a *diff preview* (what would change) without touching DB. Only `dry_run=false` executes — and orchestrator will only pass `false` after user approval webhook fires.

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/mcp/tools/write_hypothesis.py` — `research.hypothesis.create / patch / retire` (with dry_run)
  - `bifrost-research/src/bifrost_research/mcp/tools/write_backtest.py` — `research.backtest.run_event_query` (with dry_run showing planned params)
  - `bifrost-research/tests/mcp/test_write_tools.py`
- **modify**:
  - `bifrost-research/src/bifrost_research/mcp/server.py` — register write tools; add a global `require_approval=True` middleware for write tools

### Diff preview shape

```json
{
  "ok": true,
  "data": {
    "diff_kind": "create_hypothesis",
    "preview": { "title": "...", "thesis": "...", "symbols": [...] },
    "impact": { "creates_row": true, "table": "research.hypothesis" },
    "dry_run": true
  }
}
```

### Verify

Standard `bifrost-research` trio.

### Acceptance

- Every write tool returns a valid `diff_preview` when `dry_run=true`
- With `dry_run=false` **without** an approval token → tool rejects with `403: approval token required`
- `test_write_tools.py` covers both dry_run and full-run paths

---

## Phase RS-E4.2 · Approval Token Flow

- **Repo**: `bifrost-research`
- **Depends on**: RS-E4.1
- **Can run in parallel with**: RS-E4.3
- **Sign-off**: not required

### Goal

Server-side approval token: FE user clicks "Approve" on a diff card → BE issues a short-lived (60s) HMAC-signed token bound to `(action_id, user, tool, input_hash)` → FE echoes token back with the actual `dry_run=false` call.

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/copilot/approvals.py` — token issuer + validator
  - `POST /research/copilot/approve` — request approval, returns token
  - `bifrost-research/tests/api/test_approval_flow.py`
- **modify**:
  - `bifrost-research/src/bifrost_research/api/copilot.py` — wire token validation

### Verify

`make test` covers token replay / expiry / mismatch scenarios.

### Acceptance

- Reusing a token → HTTP 409 (already consumed)
- Expired token → HTTP 410
- Tool input hash mismatch → HTTP 400 (tampering)

---

## Phase RS-E4.3 · Diff Card UX in Copilot

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: RS-E4.1 (contract), RS-E4.2 (approval flow)
- **Can run in parallel with**: RS-E4.2
- **Sign-off**: not required

### Goal

When Copilot emits a `tool_call` with `dry_run=true`, FE renders a `DiffApprovalCard` instead of a plain tool-call card. Shows preview, Approve / Reject buttons.

### Files

- **new**:
  - `src/components/cockpit/DiffApprovalCard.tsx`
  - `src/components/cockpit/DiffPayloadRenderer.tsx` — per-`diff_kind` custom rendering (create_hypothesis / run_backtest / patch_hypothesis)
- **modify**:
  - `src/components/cockpit/CopilotMessageList.tsx` — branch on tool result shape

### Verify

FE standard trio.

### Acceptance

- Approve click → subsequent tool run streams into chat as normal result
- Reject → chat shows "Action rejected by user" and offers alternatives
- No `window.confirm`; use inline card affordances

---

## Phase RS-E4.4 · End-to-end Rehearsal + Runbook

- **Repo**: both
- **Depends on**: RS-E4.3
- **Can run in parallel with**: —
- **Sign-off**: required (Wave E4 sign-off, also Program-level RS-E sign-off)

### Goal

Owner-guided rehearsal of the three canonical stories; write a `docs/COCKPIT_RUNBOOK.md`.

### Stories

1. **Morning story**: Open Cockpit → Inbox shows Morning Prep drafts → approve one → hypothesis surfaces on Home → open Copilot → ask "backtest this with long straddle, 3y" → diff card → approve → view result → save link back to hypothesis.
2. **EOD story**: 4:35pm ET open Cockpit → Inbox shows EOD verdicts → approve one that flips a hypothesis to `validated`.
3. **Ad-hoc chat story**: Open Copilot cold → "any NVDA vol trades warranted this week?" → AI cites Vol Surface residual + IV rank + event radar → user pins the answer.

### Files

- **new**:
  - `bifrost-research/docs/COCKPIT_RUNBOOK.md`
- **modify**:
  - `bifrost-research/docs/CAPABILITY_MATRIX.md` — full RS-E section
  - `bifrost-research/docs/RESEARCH_UX_DECISIONS.md` — mark D-RS-E-a…i locked
  - `bifrost-trade-frontend/docs/CAPABILITY_MATRIX.md` — Wave RS-E section

### Verify

Full stack:

```bash
cd bifrost-research && make lint && make test
cd ../bifrost-trade-frontend && npm run lint && npm run build && npm run check:legacy-css
```

Manual: run each of the 3 stories against local Vite + local MCP + local API.

### Acceptance

- All 3 stories complete without errors
- `research.ai_action_log` has entries for each turn
- No `research.hypothesis` / `research.backtest_run` row is created without a corresponding approved `ai_action_log` row
- Runbook covers: how to start each component, how to point Cursor Desktop at the MCP, how to inspect the approval flow, how to reset session

---

# Cross-cutting concerns

## D10 execution freeze — hard red lines (all Waves)

Every Phase agent MUST verify **none** of these appear in new code:

- `daemon` control / scale-up
- `ib:operator:cmd` write
- `place_order` / order routing
- `POST /control/*`
- Writes to `bifrost_dev` / `bifrost_stg` / `bifrost_prod` (Trade DBs)
- Cross-environment writes (Research is Golden Source only)

Every MCP write tool + every Copilot orchestrator route must include a static import-time assertion or CI test that fails if any of the above symbols are referenced.

## Performance budget

| Metric | Target |
|--------|--------|
| Cockpit drawer open latency | < 60 ms |
| Copilot first token | < 1500 ms |
| Copilot tool call round-trip | < 500 ms (MCP local) |
| MCP tool DB query | < 300 ms p95 |
| Approval token issue → validate | < 200 ms |

## Cost budget (AI tokens)

Per D-RS-E-i:

- Dev: hard cap $2/day server-side
- Prod: raise to $10/day after E4 rehearsal passes
- Per-turn max tokens: 4096 out, 32K context in
- Fallback provider (Ollama) auto-activates on cap breach if configured

## Dev inner loop (per bifrost-trade-frontend D-IL1)

All UI acceptance runs on local Vite `:5173` against DEV API (`:30882`) + local MCP (`:8796`). **Do not** pin FE to prod API during E development.

## Owner sign-off gates

| Gate | Precondition |
|------|--------------|
| End of Wave RS-E1 | D-RS-E-a/b locked; static Cockpit fully usable without AI |
| End of Wave RS-E2 | D-RS-E-c/d/h/i locked; Copilot answers ≥ 3 canned questions with tool calls |
| End of Wave RS-E3 | D-RS-E-e/f/g locked; both agents produce drafts; Inbox approval works |
| End of Wave RS-E4 | 3-story rehearsal signed off; audit log complete |

---

# Deferred / Out of scope

| Item | Reason | Revisit |
|------|--------|---------|
| **Autonomous Agent** (RS-E5) | ROI insufficient for 2-person team; audit cost > time saved | Never for this program |
| **Wave RS-D** (Jupyter / MLflow productionization) | Cockpit + Copilot covers ~80% of small-team Jupyter value | After E4 signoff, re-evaluate need |
| **Voice mode** (STT/TTS) | Novelty; premature for 2 users | After 3 months of E4 use |
| **Multi-user collaboration** (shared cockpit sessions, mentions) | Single-user team | If team grows past 3 |
| **Mobile Cockpit** | Desktop-only research workflow | Post GA |
| **Vector-DB memory** (long-term episodic recall) | Simple `ai_action_log` + `hypothesis` covers first 6 months | If chat quality degrades |
| **Auto-triggered backtests on discovery hits** | Requires E5-level autonomy | Never for this program |
| **AI-written Analyze pages** (e.g. "generate a custom Lab") | Overengineering | Never for this program |

---

# Version bump plan

| Wave | `bifrost-research` version | Notes |
|------|---------------------------|-------|
| Current (post RS-C4) | 0.13.0 | Wave RS baseline |
| RS-E1 | 0.13.0 | FE-only; no BE bump |
| RS-E2 | **0.14.0** | MCP server + Copilot orchestrator |
| RS-E3 | **0.15.0** | DDL `ai_action_log` + `ai_draft` + agents |
| RS-E4 | **0.16.0** | Write MCP tools + approval token flow |

---

# Program-level acceptance (Owner sign-off criteria)

Wave RS-E is DONE when all of the following hold:

1. ✅ Cockpit drawer available on every `/research/*` page with tabs (Pins · Context · Actions · Copilot · Settings · Inbox)
2. ✅ Cockpit pins survive refresh (localStorage) + hypothesis pins survive session (server-side)
3. ✅ Cockpit Copilot answers ≥ 3 canned questions with tool citations and source-link jump targets
4. ✅ Morning Prep CronJob produces drafts on trading days without error for 5 consecutive sessions *(ops soak — CronJob manifests shipped; Owner soak pending)*
5. ✅ EOD Review CronJob produces drafts on trading days without error for 5 consecutive sessions *(ops soak — CronJob manifests shipped; Owner soak pending)*
6. ✅ Inbox approval flow: for each proposed AI action, `research.ai_action_log` has an audit entry with `status='executed'` if approved or `status='rejected'` if dismissed
7. ✅ Interactive write tools: Copilot chat can propose `create hypothesis` / `run backtest` / `patch hypothesis`, all approved through diff card
8. ✅ No Trade DB row is written by any Cockpit / Copilot code path (grep validated)
9. ✅ No daemon / ib_operator / place_order symbol appears in `mcp/` or `copilot/` (grep validated)
10. ✅ D10 freeze rules pass in CI

---

# Ready to execute?

Before RS-E1 starts, Owner must:

1. Sign off on **D-RS-E-a through D-RS-E-i** (9 decisions above)
2. Confirm cost cap in D-RS-E-i (dev $2/day starting cap OK?)
3. Choose default provider from D-RS-E-c (Claude 4.5-sonnet recommended)
4. Confirm MCP transport (D-RS-E-h: HTTP + SSE recommended)
5. Optional: pre-authorize batch-mode auto-advance for the whole program

After sign-off, RS-E1 → RS-E4 execute per the phase-execution protocol.
