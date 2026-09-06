# Research Loop Automation — Analyze consolidation + Copilot linkage

**Program**: `research-loop-automation` · **Blueprint**: `bifrost-platform/config/programs/active/research-loop-automation.yaml` · **Skill**: `.claude/skills/research-loop-automation/SKILL.md` (Cursor twin under `.cursor/skills/`)
**Repos**: `bifrost-research` (engines · API · harness · MCP · Dagster) · `bifrost-trade-frontend` (Research pages) · `bifrost-platform` (Trust level only, via Console)
**D10**: BLOCKED throughout. Everything here is research drafts, advisory verdicts and page structure. `order_intent` is never auto-accepted; nothing touches the Trade DB, the daemon's operator command stream or live order placement.

---

## Program Rationale

The 2026-09-06 survey (`bifrost-research` 0.66.1 on DEV) found Research complete in breadth and open in the middle:

1. **The Loop does not learn.** The daily harness screens 3,475 SEPA names and proposes 8, but every stage after Screen is degraded on DEV: the LLM plan falls back to the heuristic template on every run (`deepseek-reasoner` + 15 s timeout → timeout or unparseable JSON), Persona eval runs in heuristic mode and returns the same verdict for all eight candidates, Trust L0 is not granted so batch auto-accept is skipped, and no hypothesis has ever been resolved (36 active · 0 validated · 0 rejected). Candidate outcomes at T+1/5/20 are already computed — the pool is simply too young to have settled — and Morning/EOD agents already run under Dagster; the EOD agent just never proposes anything but "keep active" because it has no outcome rule.
2. **Twelve Analyze pages, one analysis each.** Every page reduces to "a threshold on one number → a verdict sentence", and the thresholds disagree with the engines that measure them: IV Radar says *sell premium bias* at IV Rank ≥ 60 while Signal Decay and Scan classify *hot* at ≥ 80. Track record (`stock_signal_lens_hit_daily`) exists for only three lenses and is not shown next to any verdict. Similar-regime neighbours include unsettled dates and consecutive days. Concrete defects: NVDA VRP exhibit is null although NVDA has option data; the Daily Brief narrative says "below E[close]" when spot is above; the Brief's Opportunity card is not symbol-scoped; terrain gamma zone degenerates to low = high; Order Sentiment renders a verdict on zero tape.
3. **Copilot sees four lenses.** The Exhibit contract covers vrp / iv_rank / terrain / order_sentiment; the analyze specialist's tools are VRP, surface and OpEx; Copilot cannot read a harness run, its funnel or its evidence; pages hand Copilot a prefilled prompt and get links back.

The three themes the Owner set — **Harness fully automated**, **Analyze classified and sharpened**, **Copilot wired into both** — share one foundation: a single lens vocabulary that pages, engines, Copilot and the harness all read. Wave A builds it; Waves B–D build on it.

### What the survey got wrong (corrected here)

- Signal Decay 20-day evaluation is not broken: `evaluated_20d = 0` only at the page's default 30-day window, which is shorter than the horizon it reports. At 60/90 days it evaluates 163/301 triggers (hit 62%/56%). Fix is the default window, not the engine.
- Candidate outcomes already cover horizons 1/5/20 (`DEFAULT_HORIZONS`); T+5/T+20 will settle as the pool ages.
- Morning Prep and EOD Review already run as Dagster aux assets (11:30 / 21:30 UTC); their CronJobs are suspended by design.

## Owner Decisions (2026-09-06 · locked)

| ID | Decision |
|----|----------|
| D-RLA-1 | Analyze navigation consolidates **12 → 5**: Vol regime · Dealer levels · Scenario model · Flow · Option Discovery. Contract Greeks moves to Data. Old routes redirect. |
| D-RLA-2 | Unattended leash accepted: research draft kinds auto-accept under quality gates; hypotheses auto-resolve by outcome rule when unambiguous; one daily digest replaces per-candidate cards; Inbox keeps policy changes and order intents. |
| D-RLA-3 | LLM budget approved for Persona eval on the daily Cron. **Two models evaluate in parallel — DeepSeek and OpenAI** — each with its own daily cap; auto-accept requires agreement. |
| D-RLA-4 | Flow (Order Sentiment / Multi-leg) is subscription-limited: options trades / quotes are 403 on the current Massive plan (`market-data-subscription-focus`). Keep the page as an honest placeholder that states the reason, with the OI proxy behind a clearly labelled section, so the real tape drops in later without a rebuild. |
| — | P0 hygiene (research DDL apply · Dagster image alignment) is **out of scope** — a separate session owns the Massive plugin upgrade and the Owner asked to avoid overlap. |

Defaults applied without a further question (override via policy knobs or env):

| Knob | Default | Where |
|------|---------|-------|
| Lens bands | hot ≥ 80 · cold ≤ 20 (engine truth) · lean bands 60–80 / 20–40 for page wording | `lenses/registry.py` |
| Persona models | `deepseek-chat`, `gpt-4o-mini` | `PERSONA_EVAL_MODELS` |
| Daily caps | 2.0 USD per provider | `PERSONA_EVAL_DAILY_CAP_USD_DEEPSEEK` / `_OPENAI` |
| Plan model | `deepseek-chat` (json mode, 60 s) → `gpt-4o-mini` → heuristic | `plan_llm.py` |
| Resolution rule | horizon 20 sessions · validated at excess ≥ +3% vs SPY · rejected at ≤ −3% · else draft | `policy_json.resolution` |
| Auto-accept gate | models agree ∧ validate ≠ block ∧ evidence present ∧ source hit-rate known | `batch_orchestrate.py` |

## Multi-Agent Execution Guide

Every phase is written to be picked up by an independent agent without additional context. Each declares `depends_on`, `files`, `data_model_changes`, `api_contract_changes`, `verify_cmd`, `acceptance`, `sign_off`. Follow `.claude/skills/phase-execution/SKILL.md`; in batch mode, sign-off phases still pause for the Owner.

Release facts that bite (from `research-release` skill): MCP tool changes ship in **`research-mcp`**, harness changes run from the image pinned in **`k8s/engines/cronjob-harness.yaml`**, agent/aux-asset changes run from the **`-dagster`** image in `k8s/orchestration/dagster.yaml`. A phase that touches any of these must bump that pin, in that order: image → registry tag exists → manifest.

**Parallelization snapshot**

- Wave A: A1 → (A2 ∥ A3 ∥ A4) → A5
- Wave B: B1 ∥ B2 ∥ B3 (independent of Wave A) → B4 after B1
- Wave C: C1 after A5 · C2 after A5 + C1 · C3 after A2 + C1
- Wave D: D1 after B2 · D2 after A2 + B3 · D3 after B2 + B3 + D2 · D4 after D2 + C1

---

## Wave A — Foundation: one lens vocabulary

### Phase A1 · Lens registry

- **Repo**: `bifrost-research` · **depends_on**: [] · **sign_off**: api
- **Goal**: every threshold that turns a number into hot / cold / neutral lives in one registry, and every consumer reads it.
- **Files**
  - new: `src/bifrost_research/lenses/__init__.py`, `src/bifrost_research/lenses/registry.py` (`LensSpec`: id · label · source table · value column · unit · bands · side semantics · horizons · page route · data dependency · notes; `classify(lens, value)`; `public_registry()`), `src/bifrost_research/api/lenses.py` (`GET /research/lenses`), `src/bifrost_research/mcp/tools/lenses.py` (`research.lenses.list`), `tests/lenses/test_registry.py`
  - modify: `engines/signal_hit/build.py` (classifiers delegate; constants removed), `engines/scan/build.py` (`flag_from_score` delegates), `engines/alert_scan/entry.py` (lens-based thresholds read registry; `hit_rate_drop` 8 pp stays an alert rule), `api/similar_regime.py` (lens list from registry), `api/app.py`, `mcp/server.py` registration
- **Lenses**: `iv_rank`, `iv_percentile`, `vrp`, `skew` (surface `atm_slope`, |slope| ≥ 0.25 hot · ≥ 0.12 lean), `term_slope`, `gex_regime` (net GEX sign + zero-gamma offset; categorical), `opex_pin` (|distance| ≤ 1%), `terrain_regime` (categorical), `momentum`, `sepa`, `order_sentiment` (±30 · `data_dependency: option_trades_tape`), `forecast_path` (settlement hit).
- **api_contract_changes**: `GET /research/lenses` (new). No existing route changes.
- **verify_cmd**: `cd bifrost-research && make lint && make test && make check-code-health`
- **acceptance**: registry lists ≥ 10 lenses with bands and routes; `signal_hit` and `scan` classification tests pin equality with registry output; no numeric threshold literal remains in those two engines; code-health 5/5.

### Phase A2 · Exhibit for every lens

- **Repo**: `bifrost-research` · **depends_on**: [A1] · **sign_off**: api
- **Goal**: the page's verdict and Copilot's answer come from the same object.
- **Files**
  - modify: `api/exhibit.py` — readers for `skew`, `term_slope`, `gex_regime`, `opex_pin`, `forecast_path`, `momentum`, `sepa` alongside the existing four; every exhibit gains `verdict {band, label}` from the registry, `track_record {n, hit_5d, hit_20d, by_side, window_days}` from `stock_signal_lens_hit_daily`, `similar {n, median_fwd, p25, p75}` for numeric lenses (resolved-only neighbours), and a `caveats` entry naming a missing data dependency; `mcp/tools/exhibit.py` (lens set = registry); `copilot/agents/graph.py` (analyze specialist tool filter += `research.exhibit.get`, `research.lenses.list`, signal-decay and similar-regime reads); `copilot/agents/instructions/analyze.md`
  - new: `tests/api/test_exhibit_lenses.py`
- **api_contract_changes**: `GET /research/exhibit/{lens}` accepts every registry lens; response adds `verdict`, `track_record`, `similar` (additive).
- **verify_cmd**: `cd bifrost-research && make lint && make test && make test-mcp`
- **acceptance**: on DEV, every lens returns 200 for NVDA and SPY with `verdict` and `track_record`; the analyze specialist answers a GEX question citing the exhibit tool.

### Phase A3 · Signal Decay lens expansion + similar-regime hygiene

- **Repo**: `bifrost-research` · **depends_on**: [A1] · **sign_off**: api
- **Files**
  - modify: `engines/signal_hit/entry.py` (+ `skew`, `gex_regime`, `terrain_regime`; `order_sentiment` only when `data_source = option_trades_tape`), `engines/signal_hit/build.py` (side semantics per registry), `api/signal_decay.py` (lens list from registry; `window_days` default stays 30 — the FE default moves to 60 in A5), `api/similar_regime.py` (resolved-only neighbours, de-clustered ≥ 5 sessions apart, `summary` block)
  - new: `scripts/backfill_signal_hit.py --lens <id> --days 252`, `tests/engines/test_signal_hit_lenses.py`, `tests/api/test_similar_regime_hygiene.py`
- **data_model_changes**: none (`stock_signal_lens_hit_daily` keyed by lens already).
- **acceptance**: `GET /research/signal-decay?lens=skew&window_days=90` returns triggers with 5d/20d evaluation; similar-regime rows all carry `fwd_return`; no two neighbours within 5 sessions.

### Phase A4 · Analysis defects

- **Repo**: `bifrost-research` · **depends_on**: [] · **sign_off**: api
- **Fixes** (each with a regression test):
  1. `engines/brief/synth.py` — narrative wording follows the sign of spot − E[close]; Opportunity card prefers the symbol's own SEPA row and says so when there is none; sentiment card reads "no tape" unless `data_source = option_trades_tape`.
  2. `engines/forecast/terrain.py` — `expected_close_and_gamma_zone` returns `None` bounds (with caveat) when put/call walls are missing instead of low = high.
  3. `engines/vrp/compute.py` — `fetch_atm_iv_30d` interpolates between the two expiries bracketing 30 DTE (falls back to nearest); `compute_fwd_ret_20d` wired as a Dagster aux asset after `engines_vrp` so `fwd_ret_20d` stops being permanently NULL.
  4. `api/exhibit.py` `order_sentiment` — caveat and `verdict = None` when the tape is absent.
- **acceptance**: NVDA brief narrative sign correct on DEV; NVDA VRP exhibit non-null; gamma zone never degenerate; sentiment exhibit for SPY carries the tape caveat.

### Phase A5 · Pages read the registry

- **Repo**: `bifrost-trade-frontend` · **depends_on**: [A1, A2, A3, A4] · **sign_off**: **Owner** (verdict wording changes on every lab)
- **Files**
  - new: `src/api/research/lenses.ts`, `src/hooks/useLensRegistry.ts`, `src/lib/lensVerdict.ts` (+ test) — band → tone / label / wording; schema entry in `src/lib/schemas/research.ts`
  - modify: `components/research/AnalyzeVerdictStrip.tsx` (+ `trackRecord` line: "hot side hit 5d 68% · 20d 62% · n=22"), `components/research/SimilarRegimeCard.tsx` (distribution summary, resolved-only note), `components/research/CompositeRegimeRibbon.tsx` (lenses from registry), `pages/research/analyze/{IvRadar,VrpLab,VolSurfaceLab,OpExCycleLab,GexIntraday,AnalysisModel,OrderSentiment}Page.tsx` (local threshold functions removed; verdict from exhibit + registry), `pages/research/validate/SignalDecayPage.tsx` (default window 60; 20d column always visible), `CopilotAutoInsightChip` mounted on the labs that lack it
- **verify_cmd**: `cd bifrost-trade-frontend && npm run lint && npm run test:run && npm run build && npm run check:legacy-css && npm run check:code-health`
- **acceptance**: on `:5173` every lab's verdict label equals the band in `GET /research/lenses` for the symbol's exhibit; Signal Decay shows 20d numbers without touching the window; a grep for `>= 60`, `>= 80`, `<= 20` style literals in `pages/research/analyze/*.tsx` returns nothing.

---

## Wave B — Harness: a real judge and a closed circuit

### Phase B1 · LLM plan repair

- **Repo**: `bifrost-research` · **depends_on**: [] · **sign_off**: api
- **Files**: modify `copilot/harness/plan_llm.py` — `DEFAULT_MODEL = "deepseek-chat"`, `response_format: {type: json_object}`, `DEFAULT_TIMEOUT_SECONDS = 60`, provider chain DeepSeek → OpenAI (`gpt-4o-mini` through `copilot/models.py`) → heuristic; trace records `generated_by`, `llm_model`, `attempts[]`; `copilot/harness/planning.py` (fallback reason names the last failure); `tests/copilot/test_plan_llm_chain.py` (httpx mocked: timeout → OpenAI → success; both fail → heuristic with reason).
- **acceptance**: unit chain tests; on the next DEV Cron run `plan.generated_by == "llm"` with the model named.

### Phase B2 · Two models judge every candidate

- **Repo**: `bifrost-research` + `bifrost-trade-frontend` · **depends_on**: [] · **sign_off**: **Owner** (spend and behaviour change on the daily Cron)
- **Files**
  - modify: `copilot/harness/persona_eval.py` — `PERSONA_EVAL_MODELS` (default `deepseek-chat,gpt-4o-mini`); per symbol, run the verdict agent once per model concurrently (`asyncio.gather`, per-model timeout); verdict rows carry `model`; consensus: `net_stance` = the agreed stance, else `dissent`; `validate_stance` = the most severe across models (a block from either model blocks); `auto_approve_eligible` only when all models agree and none blocks; a model that fails is recorded as `heuristic_fallback` for that model and counts as dissent — never as agreement. `copilot/rate_limit.py` — per-provider counters and caps `PERSONA_EVAL_DAILY_CAP_USD_{DEEPSEEK,OPENAI}`; spend persisted per run into `research.ai_action_log` so the cap survives process restarts. `copilot/harness/runtime.py` / `trace.py` — `persona_eval {mode, models[{model, elapsed_ms, cost_usd, fallback}], agreement}`. `k8s/engines/cronjob-harness.yaml` — `BIFROST_PERSONA_EVAL_AGENTS=1`, `PERSONA_EVAL_MODELS`, both caps, `BIFROST_PERSONA_EVAL_TIMEOUT_S=240`; image pin bump.
  - FE: `components/research/harness/CandidateBatchBody.tsx` (per-model stance chips + `agree` / `dissent` tag), `HarnessPipelineStepper.tsx` (Judge stage shows models, elapsed, cost), `lib/harness/harnessTrace.ts` types.
  - new: `tests/copilot/test_persona_consensus.py`
- **data_model_changes**: `research.ai_action_log` gains `provider text` and `cost_usd numeric` if absent (DDL in `schema/ddl.py`; documented in the DDL changelog). **Architecture-level — approved by D-RLA-3.**
- **acceptance**: DEV Cron run trace shows `mode: agent`, two models, a non-uniform verdict distribution across the batch, cost per provider under its cap; a dissent is visible in the Inbox card.

### Phase B3 · Hypotheses resolve themselves

- **Repo**: `bifrost-research` + `bifrost-trade-frontend` · **depends_on**: [] · **sign_off**: **Owner** (first automatic status change)
- **Files**
  - modify: `copilot/harness/policy_schema.py` (`resolution {horizon_days, validate_excess, reject_excess, benchmark}` + `POLICY_SUGGESTION_WHITELIST`), `copilot/agents/eod_review.py` — for hypotheses with candidate lineage (`origin_ref.candidate_id`), read `research.candidate_outcome` at the horizon: unambiguous → set status directly with `conclusion` text and `resolution_json` evidence, log to `ai_action_log`, post an informational `eod_verdict` briefing; ambiguous or no lineage → draft as today. `repositories/hypothesis.py` (`resolve()`), `schema/ddl.py`, `orchestration/research_aux_schedules.py` (candidate_outcome asset ordered before eod_review), `k8s/orchestration/dagster.yaml` pin.
  - FE: `components/research/harness/PolicyKnobEditor.tsx` (resolution knobs), `pages/research/loop/HypothesisBoardPage.tsx` + `components/research/HypothesisCard.tsx` (resolution evidence line), `hooks/useLoopOverview.ts` (learn segment counts resolved hypotheses).
- **data_model_changes**: `research.hypothesis.resolution_json jsonb` (new column). **Architecture-level — approved by D-RLA-2.**
- **acceptance**: once the first candidates reach 20 sessions, validated + rejected > 0 with evidence attached and no Owner click; ambiguous cases still arrive as drafts; Research Home "learn" segment is not starved.

### Phase B4 · Every objective runs, evidence is per symbol

- **Repo**: `bifrost-research` · **depends_on**: [B1] · **sign_off**: api
- **Files**: `k8s/engines/cronjob-harness.yaml` (drop `BIFROST_LOOP_OBJECTIVE_ID`; `--schedule daily_open`), `copilot/harness/entry.py` (per-objective failure isolation; summary line per objective), `copilot/harness/validate_hook.py` + `engines/backtest/event_query.py` (verify with a fresh run that 0.65.8 made the evidence symbol-scoped; if five hypotheses still carry the same `n_events / win_rate / avg_pnl`, scope the event set to the hypothesis symbol and choose the template by the hypothesis' instrument), `tests/copilot/test_validate_hook_symbol_scope.py`.
- **acceptance**: both active objectives have runs on DEV; evidence numbers differ across symbols; Morning IV Hot Watch produces its first batch.

---

## Wave C — Analyze: twelve pages into five hubs

### Phase C1 · Hubs and redirects

- **Repo**: `bifrost-trade-frontend` · **depends_on**: [A5] · **sign_off**: **Owner** (navigation)
- **Mapping**

| Hub route | `?view=` | Former pages |
|-----------|----------|--------------|
| `/research/vol-regime` | `iv-rank` (default) · `vrp` · `skew` | IV Radar · IV-RV Spread · Vol Surface |
| `/research/dealer-levels` | `gex` (default) · `opex` | GEX Intraday · OpEx Cycle |
| `/research/scenario` | `model` (default) · `sessions` · `playbook` | Analysis Model · Forecast Sessions · Intraday Playbook |
| `/research/flow` | — | Order Sentiment (+ `#multi-leg`) — placeholder per D-RLA-4 |
| `/research/discovery` | unchanged | Option Discovery |
| Data group | — | Contract Greeks (`/research/greeks` unchanged) |

- **Files**
  - new: `pages/research/analyze/hub/LabHub.tsx` (one header: `PageHeader` + `ResearchContextBar` + `CompositeRegimeRibbon` + view tabs; `?view=` in the URL), `pages/research/analyze/volRegime/{VolRegimePage.tsx,IvRankSection.tsx,VrpSection.tsx,SkewSection.tsx}`, `pages/research/analyze/dealerLevels/{DealerLevelsPage.tsx,GexSection.tsx,OpexSection.tsx}`, `pages/research/analyze/scenario/{ScenarioPage.tsx,ModelSection.tsx,SessionsSection.tsx,PlaybookSection.tsx}`, `pages/research/analyze/flow/FlowPage.tsx` (placeholder card: reason, link to `plugin-options-tape` follow-on; the OI proxy view under a labelled collapsible), `pages/research/analyze/hub/labHub.test.tsx`
  - move: each former page body becomes its section (mechanical; behaviour unchanged); former page files deleted
  - modify: `lib/router.tsx` (hub routes; old paths → `Navigate` with the matching `?view=`), `layout/navConfig.ts` (Analyze: 5 items; Contract Greeks under Data), `layout/AppHeader.tsx` titles, `pages/settings/uiDesignSystem/scopeRegistry.ts`, `components/research/CompositeRegimeRibbon.tsx` `LENS_ROUTES`, `pages/research/home/LoopOverviewStrip.tsx` `SEGMENT_HREF` (learn → Signal Decay unchanged), every `to: '/research/...'` in exhibits and `useDailyVerdict`
- **Constraints**: no file over 800 lines (the ratchet has zero headroom — sections stay separate files); no duplicate module-scope function names beyond three; `check:legacy-css` module-placement rules (hub-only components stay under `pages/research/analyze/`).
- **verify_cmd**: `cd bifrost-trade-frontend && npm run lint && npm run test:run && npm run build && npm run check:legacy-css && npm run check:code-health`
- **acceptance**: Analyze shows five entries; every old URL lands on the right hub view; every former page's content is reachable; Owner smoke on `:5173`.

### Phase C2 · Per-lens analytical depth

- **Repo**: `bifrost-research` + `bifrost-trade-frontend` · **depends_on**: [A5, C1] · **sign_off**: api (per item), Owner smoke at the end
- **Items** (each a sub-phase with its own test and acceptance)
  1. **Skew** — normalised metric (25Δ risk reversal / ATM, or slope percentile against the symbol's own 252-day history) replacing raw slope thresholds; term-structure slope verdict (contango / backwardation) from `/research/vol-surface/term-structure`; "rich / cheap strikes" list from residuals for a premium seller.
  2. **OpEx** — verdict names the pin magnet strike, its distance and the historical pin rate (from `pin-analysis`), not only the calendar; feeds the `opex_pin` lens.
  3. **GEX** — gamma-regime verdict (positive / negative net gamma, spot vs zero-gamma) with the realised-vol link (VRP) it implies; feeds `gex_regime`.
  4. **Scenario** — reliability calibration per regime from `stock_backtest_settlement` (forecast probability vs realised path hit), shown next to the four probabilities; Playbook invalidation line reads the calibration.
  5. **IV Radar** — bucket track record (hot / cold hit rates) on the radar table; similar-regime distribution in the verdict.
  6. **VRP** — `fwd_ret_20d` populated (A4) surfaces as the lab's own hit-rate line.
- **acceptance**: every hub verdict strip shows band · track record · similar distribution; the scenario hub shows a calibration table with n per regime.

### Phase C3 · Daily Brief on exhibits

- **Repo**: `bifrost-research` + `bifrost-trade-frontend` · **depends_on**: [A2, C1] · **sign_off**: api
- **Files**: `engines/brief/synth.py` (cards built from the exhibit readers — one source), `pages/research/discover/DailyBriefPage.tsx` + `hooks/useDailyVerdict.ts` (links to hub views; lamps from exhibit freshness).
- **acceptance**: a Brief card and the hub view it opens show the same numbers and the same verdict for the same symbol and date.

---

## Wave D — Copilot in the loop, and the leash

### Phase D1 · Copilot reads and explains a run

- **Repo**: `bifrost-research` + `bifrost-trade-frontend` · **depends_on**: [B2] · **sign_off**: api
- **Files**: new MCP read tools `research.loop.list_runs`, `research.loop.get_run` (plan · funnel steps · persona verdicts per model · evidence · report), `research.loop.explain_candidate`; write tool `research.loop.run_objective` (dry-run default, approval-gated like the other loop writes); `copilot/agents/instructions/{loop_curator,verdict}.md`; FE: Harness drawer and `CandidateBatchBody` get "Ask Copilot" with the run / candidate as the snapshot (`AskCopilotButton` intent).
- **acceptance**: from the Harness drawer, "why was WT proposed and what would unmake it" is answered from the run's own evidence with tool citations.

### Phase D2 · One daily digest

- **Repo**: `bifrost-research` + `bifrost-trade-frontend` · **depends_on**: [A2, B3] · **sign_off**: api
- **Files**: new `copilot/agents/daily_digest.py` (verdict agent over: exhibits for holdings ∪ today's candidates, loop state, resolutions since yesterday, dissents) producing one `daily_digest` briefing per day; `morning_prep` folded into it; Dagster aux asset at 11:30 UTC; FE: Inbox Briefings shows the digest first with candidate batches folded beneath; `InboxBanner` in the Copilot panel opens on the digest.
- **acceptance**: one digest per trading day on DEV; the Inbox opens on it; per-candidate cards are reachable but not the first thing seen.

### Phase D3 · Unattended, on a leash

- **Repo**: `bifrost-research` (+ Console action by the Owner) · **depends_on**: [B2, B3, D2] · **sign_off**: **Owner**
- **Steps**: auto-accept gate in `batch_orchestrate.py` = models agree ∧ validate ≠ block ∧ evidence present ∧ source track record known (knob `min_source_hit_rate`, default 0.45); the Owner sets `research-loop-batch` to **L0** in Console → Agent Governance (Set level; `PUT /api/v1/agent/governance/trust-overrides/research-loop-batch`); weekly `policy_suggestion_from_outcomes` as a Dagster aux asset (Sunday 22:00 UTC) so the rules get a proposal from settled outcomes; Inbox default filter = Decisions.
- **acceptance**: a day passes with candidates proposed, judged, accepted into hypotheses and reported in the digest without an Owner click; policy and order-intent drafts still wait; the weekly rule proposal appears with its evidence.

### Phase D4 · Copilot verdicts back on the pages

- **Repo**: `bifrost-trade-frontend` + `bifrost-research` · **depends_on**: [D2, C1] · **sign_off**: api
- **Files**: `CopilotAutoInsightChip` fed by the digest's per-symbol verdicts on every hub; chat-side proposals (existing `research.loop.propose_candidate`, `research.hypothesis.create`) surface as chips on the symbol's hub view with the approval state.
- **acceptance**: a verdict produced in chat for NVDA is visible on the NVDA vol-regime view within one refresh, with its approval state.

---

## Verification (program level)

```bash
cd bifrost-research && make lint && make test && make test-mcp && make check-code-health
cd bifrost-trade-frontend && npm run lint && npm run test:run && npm run build && npm run check:legacy-css && npm run check:code-health
bash scripts/check-agent-config-parity.sh
```

DEV acceptance: Vite `:5173` against `192.168.10.73:30882` (D-IL1); Cron behaviour read from `kubectl logs -n research job/research-harness-<id>`; release through the `research-release` skill (api · mcp · harness Cron · dagster pins as the phase requires).
