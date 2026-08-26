# Research UX Wave R1–R8 — Owner Decisions (defaults applied)

| ID | Question | Default (implemented) |
|----|----------|----------------------|
| D-R4-a | Event LLM tagger | `EventTagger` interface; heuristic default; optional `EVENT_RADAR_LLM_PROVIDER` |
| D-R4-b | Macro calendar source | Manual CSV drop (`macro_ingest` scheduler) |
| D-R5-a | Polygon options tape | Scaffold ingest + flow reads tape when rows exist |
| D-R6-a | News auto-ingest | Manual drop only (existing event radar ingest) |
| D-R6-b | Schwab settlement | Use `market.stock_daily` close (no Schwab API) |
| D-R6-c | LLM token dashboard | Basic session cost fields in forecast session metadata |
| D-R6-d | Theme registry editor | Markdown in Research-workspace (no Console page) |
| D-R7-a | Daily Brief Verdict source | R7: FE `useDailyVerdict`; **R8 (D-R8-a)**: `GET /research/daily-brief/synth` primary, 404/503 → R7 fallback |
| D-R7-b | Intraday replay slider | Removed in R7; **R8**: Session Timeline + `selectedIdx` (D-R8-c) |
| D-R8-a | Synth vs multi-query | Single synth API primary; FE fallback to R7 parallel queries |
| D-R8-b | CronJob L1 trigger | Trade FE EmptyHint → `POST /api/v1/research/cronjobs/{id}/trigger` (operator token) |
| D-R8-c | Session Timeline data | Only `GET /terrain/intraday` (spot + regime bands) |
| D-R9-a | Symbol search data source | Market Data Plugin `GET /market/reference/tickers/search` (Golden Source `raw_market.ticker`) |
| D-R9-b | SymbolPicker placement | `bifrost-trade-frontend/src/components/symbol/` (Trade FE; not `@bifrost/ui`) |
| D-R9-c | Search UX | 250ms debounce · `staleTime` 5m · empty query shows SPX/SPY/QQQ/IWM suggestions |
| D-R9-d | Empty-state suggestions | Positions (monitor holdings) + Watchlist STK ∪ Benchmarks; row badges **Position** / **Watchlist** on matches too |
| D-RS-a | Hypothesis table location | Golden Source `bifrost_golden_source.research.hypothesis` — OLAP domain, cross-env viewable, matches D13 (Wave RS-A1 · locked) |
| D-RS-b | Sidebar restructure | **Replace** legacy 6-subGroup taxonomy with 5-stage grouping (Home / Discover / Analyze / Validate / Data) — clean break, no toggle (Wave RS-A5 · locked) |
| D-RS-c | Research home route | `/research` becomes Research Home; Daily Brief remains at `/research/daily-brief` (Wave RS-A4 · locked) |
| D-RS-d | Backtest event-query API path | `POST /research/backtest/event-query` — event-driven runs return summary + per-event trades + optional walk-forward + benchmark. Persistence in `bifrost_golden_source.research.backtest_run`; `hypothesis_id` auto-appends `run_id` to `research.hypothesis.linked_backtest_ids` (Wave RS-C4 · **implemented** in bifrost-research 0.13.0) |
| D-RS-e | Earnings event source | Priority: (1) `raw_market.corporate_action.event_type='earnings'` when populated, (2) heuristic on `features.event_signal_radar_daily` (raw_text ILIKE '%earnings%'), (3) hard-coded 9-symbol quarterly stub (NVDA/AAPL/AMZN/GOOGL/MSFT/META/TSLA/AMD/SPY). Chosen source is echoed back to FE in `event_source` field (Wave RS-C1 · **stub fallback in production**, replace when calendar table lands) |
| D-RS-f | Option leg pricing bar | `raw_market.option_daily` only carries OHLCV in Golden Source. RS-C1 uses `close` for entry/exit; RS-C2 `compute_fill_price` degrades to `close` when bid/ask=0 (backward compatible). When bid/ask columns land, `FillConfig.slippage_pct_of_spread=0.2` kicks in automatically (Wave RS-C2 · locked) |
| D-RS-E-a | Cockpit UI form-factor | Right-side collapsible Drawer (`⌘K` / Esc); Radix Sheet `side="right"` — **locked** · **implemented** Wave RS-E1.1 |
| D-RS-E-b | State persistence | Hybrid: URL for shareable subset (symbol/date via `useResearchContext`) · localStorage for UI prefs/pins · server for hypothesis state — **locked** · **implemented** Wave RS-E1.2 (pins local; hypothesis already server-backed) |
| D-RS-E-c | AI provider strategy | Multi-provider abstraction (LiteLLM / provider-agnostic) — **locked** · **implemented** Wave RS-E2 (`copilot/providers.py` Claude / OpenAI / Ollama) |
| D-RS-E-d | MCP server placement | Standalone process `research-mcp:8796` — **locked** · **implemented** Wave RS-E2 (`bifrost_research.mcp`, K8s `k8s/mcp/`) |
| D-RS-E-e | AI write approval | All writes approval-gated (diff card + click) — **locked** · **implemented** Wave RS-E3 (Inbox) + **RS-E4** (Copilot DiffApprovalCard + HMAC token) |
| D-RS-E-f | Morning / EOD scheduling | CronJob + on-demand — **locked** · **implemented** Wave RS-E3 (`cronjob-morning-prep` / `cronjob-eod-review` + Actions buttons) |
| D-RS-E-g | AI action audit log | `research.ai_action_log` table — **locked** · **implemented** Wave RS-E3 (+ `research.ai_draft`) · **RS-E4** execute/dismiss paths |
| D-RS-E-h | MCP transport | HTTP + SSE — **locked** · **implemented** Wave RS-E2 (`:8796/sse`) |
| D-RS-E-i | Cost / rate-limit | Hard daily token cap (`MAX_TOKENS_PER_DAY`) — **locked** · **implemented** Wave RS-E2 (`COPILOT_DAILY_CAP_USD` default $2/day) |
| D-RS-F-a | Copilot runtime SDK | `openai-agents` (Python) — **implemented** Wave RS-F (`agent_runtime.py` + `agents/graph.py`) |
| D-RS-F-b | MCP transport (RS-F) | Keep current **SSE** `:8796/sse` — **implemented** (`MCPServerSse` in agent graph) |
| D-RS-F-c | Default Copilot model | User preference in `localStorage` · default **`deepseek-chat`** — **implemented** (API + FE model catalog) |
| D-RS-F-d | DeepSeek transport | Direct API `https://api.deepseek.com/v1` via `DEEPSEEK_API_KEY` — **implemented** (`copilot/models.py`) |
| D-RS-F-e | Multi-agent topology | Triage + 5 specialists handoffs + Verdict `Agent.as_tool()` — **implemented** (`agents/graph.py`) |
| D-RS-F-f | SSE event schema | Extended with `agent_handoff`, `guardrail` (back-compat) — **implemented** |
| D-RS-F-g | Session storage | `research.copilot_session` + Cockpit Sessions sidebar — **implemented** |
| D-RS-F-h | Guardrail strictness | Hard reject D10 → SSE `D10_FREEZE` + `ai_action_log` — **implemented** |
| D-RS-F-i | Cockpit form-factor v2 | Settings toggle Overlay vs Dock (400px shift) — **implemented** |
| D-RS-F-j | Tracing sink | Stdout + JSONL (`RESEARCH_COPILOT_TRACE_JSONL`); optional OTLP env — **implemented** |

Override via env / spine when Owner changes policy.

> **Wave RS-E note**: **Complete** — E1 Static Cockpit · E2 Copilot + MCP · E3 Morning/EOD + Inbox · E4 interactive writes (diff + approve). See `docs/COCKPIT_RUNBOOK.md`.

> **Wave RS-F note**: **Complete** (F1–F5) — Copilot v2 on OpenAI Agents SDK, DeepSeek default, multi-agent handoffs, D10 guardrails, session persistence, Cockpit Dock/Trace/Sessions. F6 (Morning/EOD SDK migration) optional/deferred · F7 sandbox deferred. bifrost-research **0.17.0** · runbook section below.
