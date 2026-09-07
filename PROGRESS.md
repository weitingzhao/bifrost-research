## Research Loop Automation — Waves A–D — planned 2026-09-06

**Status:** 🟢 Wave C complete; Wave D in progress — D1 + D2 released as **0.74.0** (api · mcp · engines; dagster `0.74.0-dagster`), frontend D1 + D2 in `bifrost-trade-frontend` (`856ba67`, Satellite chain). Next: D3 leash (sign-off Owner) · D4 verdict chips. Plan: `docs/plans/RESEARCH_LOOP_AUTOMATION_PLAN.md`. D10 BLOCKED.

DEV acceptance 2026-09-06: every registry lens exhibits 200 with `verdict` / `track_record` / `similar` for NVDA and SPY; Brief NVDA narrative sign correct, Opportunity is NVDA's own PIVOT, sentiment card says no tape; NVDA VRP `atm_iv_30d` populated after the recompute Job; terrain gamma zone 228.85–231.15 (`walls_widened`) after the terrain recompute Job; similar-regime returns resolved-only, de-clustered neighbours with `summary`; `scripts/backfill_signal_hit.py` walked 252 sessions (706 rows: gex_regime 704 · skew 1 · terrain_regime 1 — skew / crash-risk thresholds are too strict for this universe; calibration lands in C2).

Release facts learned: `k8s/orchestration/**` is excluded from the Argo app, so `dagster.yaml` is applied by hand after the `bifrost-build-research-dagster` PipelineRun; one-off Job manifests now carry `argocd.argoproj.io/hook: Skip` (a Job's pod template is immutable, so pin bumps failed the sync).

| Wave | Deliverable | Status |
|------|-------------|--------|
| A | Lens registry · Exhibit for every lens · Signal Decay lens expansion · analysis defects · pages read the registry | **A1 ✅** (`lenses/registry.py`, `GET /research/lenses`, MCP `research.lenses.list`) · **A2 ✅ 2026-09-06** (exhibit for all 12 lenses + `terrain` alias; `verdict` / `track_record` / `similar` on every exhibit; `similar_rows` shared with the k-NN API; analyze specialist instruction) · **A3 ✅ 2026-09-06** (decay lenses skew / gex_regime / terrain_regime / order_sentiment with registry hit rules; similar-regime resolved-only + de-clustered + summary; `scripts/backfill_signal_hit.py`) · **A4 ✅ 2026-09-06** (brief narrative sign + symbol-scoped Opportunity + honest tape card; gamma zone never a point; VRP 30-DTE interpolation, VRP asset after volatility, fwd_ret_20d backfill asset) · **A5 ✅ 2026-09-06** (frontend, `bifrost-trade-frontend`: `useLensRegistry` / `useExhibit` hooks, `lib/lensVerdict.ts` band → tone / label tables, seven analyze labs take the registry band with track-record + similar evidence lines, ribbon labels from the registry, Signal Decay 90d default + Skew / Gamma / Terrain + 20d columns; verified on `:5173` against the local api; ships with the Satellite chain) |
| B | LLM plan repair · two-model Persona judgement + caps · hypothesis auto-resolution · every objective runs | **B1 ✅ 2026-09-06** (`plan_llm`: `deepseek-chat` in json mode, 60s, chain policy model → `deepseek-chat` → `gpt-4o-mini` → heuristic; every hop recorded as `llm_attempts` and the fallback reason names each hop; endpoint table shared via `copilot/models.resolve_chat_endpoint`; planner told not to echo the current policy as a suggestion; live smoke on the DEV objective: DeepSeek 2.3–3.6 s ≈ $0.0003, forced OpenAI hop 4.3 s ≈ $0.005; DEV: `plan.generated_by=llm`, `deepseek-chat`, hops in `llm_attempts` — verified on the 0.69.2 acceptance run) · **B2 ✅ 2026-09-06** (`persona_judge`: every candidate judged by `PERSONA_EVAL_MODELS` = deepseek-chat + gpt-4o-mini concurrently; `net_stance` = agreed verdict else `dissent`, validate = most severe, a failed / partial / over-cap judge is dissent; per-provider purses `PERSONA_EVAL_DAILY_CAP_USD_{DEEPSEEK,OPENAI}` seeded from and written to `research.ai_action_log` (`provider`, `cost_usd`; `action_kind=persona_eval_spend`); 0.69.1 made the judge stage policy-governed after the first LLM plan dropped it; 0.69.2 named judge timeouts, priced gpt-4o-mini by name, widened the budgets (120 s / 900 s) and gave the Cron the Trade backends. DEV acceptance run `run_1a078b3a64d5b6a59`: mode=agent, two judges 16/16 ok, 4 agree · 4 dissent · 3 blocked, LPG support / NVDA·EE caution / WT·BG·FANG·HALO dissent, deepseek $0.40 · gpt-4o-mini $0.06 under $2 caps, two ledger rows, judge stage 448 s, curator writes clean on the in-process MCP. DDL applied by hand with the owner role — `ddl-apply` Job runs as `analytics_writer` and cannot ALTER, see COCKPIT_RUNBOOK; 0.69.3 puts the judges on a turn leash `BIFROST_PERSONA_EVAL_MAX_TURNS=4` and DeepSeek at $0.50/day — Owner decision) · **B3 ✅ 2026-09-06** (`hypothesis_resolution`: candidate-born hypotheses settle by `policy.resolution` — 20 sessions, excess vs SPY ≥ +3% validated / ≤ −3% rejected, dead band drafts with the number; receipt in `hypothesis.resolution_json`, ledger row `hypothesis_auto_resolve`, informational `eod_verdict`; `resolution` in both policy whitelists; Dagster `research_eod_review_job` = `engines.candidate_outcome` → `agents.eod_review`; `origin_ref.objective_id`. DEV 0.70.0: dry-run at the policy horizon → 20 pending (earliest candidate 2026-08-29, first 20-session windows close ≈ 2026-09-27), 24 without lineage; preview at 1 session → LPG +3.43% / PAYS +4.17% validated, 6 in the dead band, nothing written. First automatic status change lands when the nightly eod job meets a closed window) · **B4 ✅ 2026-09-06** (harness Cron drops `BIFROST_LOOP_OBJECTIVE_ID` — every active `daily_open` objective runs; `entry.py` isolates failures per objective (rollback, one summary line each, exit 1 only when nothing ran); `validate_hook` picks the template by instrument (option-lens objective → `short_strangle_30d` when the option history allows, else the stock leg and says so); symbol-scoped evidence confirmed on the 0.69.2 run (HALO 0.8 / LPG 0.6 / EE 0.4 / NVDA 0.2 vs the 09-04 rows all at 0.3243). DEV 0.70.1 acceptance job: Daily Loop Stock Explorer 8 candidates (`run_1a078e5d6642c22f6`) and Morning IV Hot Watch's first batch, 3 candidates (`run_1a078ead64edd0578`), trust report recorded) |
| C | Analyze 12 → 5 hubs · per-lens depth · Daily Brief on exhibits | **C1 ✅ 2026-09-07** (frontend `bifrost-trade-frontend` `5ff3d2e`: hubs `/research/{vol-regime,dealer-levels,scenario,flow}` + Option Discovery, `?view=` tabs on one header, nine retired paths redirect with query + hash intact, Flow placeholder per D-RLA-4, Contract Greeks under Data; backend `lenses/registry.py` `page_route` and `brief/synth.py` links name the hub views — shipped in 0.71.0; verified on `:5173`, Owner navigation approved) · **C2 ✅ 2026-09-07** (backend `d4fb880` + release `0.71.0`: skew judged on the slope's percentile of the symbol's own 252-day history (`readings.slope_pctile_252d` / `history_days`, thin-history caveat under 60 days; the signal_hit skew trigger uses the same percentile, so the 1-trigger-in-179-days defect from A3 is gone); `term_slope` reads near − far as backwardation / contango; `opex_pin` carries `history_summary.{cycles,pinned,pin_rate,median_abs_distance}`; `gex_regime` carries `readings.vrp_link` (RV20 vs IV30, VRP percentile, `consistent`); `vrp` carries `history_summary.fwd20_by_band`; new `GET /research/signal-decay/by-symbol` and `GET /research/forecast/calibration`. Frontend `870eaa5`: `lib/analyzeDepth.ts` text builders, Skew narrative + Own pctl / Term signals + term-structure and rich/cheap-strike lines + ungraded extremes table, OpEx pin-magnet lead, GEX realised-vol sentence, VRP own 20d record, IV Radar `Own hit 5d / 20d` column, Sessions calibration-by-regime table, Playbook LIVE-card reliability line. DEV facts on NVDA 2026-09-04: skew percentile 0 on 9 history days (cold, caveated); term backwardation +4.1 pts (28d 41.8% vs 77d 37.6%); pin magnet 220, 1 settled cycle; gex positive regime with RV20 44.8% > IV30 33.7% → `consistent=false`; VRP fwd20 bands empty (no settled extremes yet); calibration: range regime 0 / 15 hits vs 40% claimed) · **C3 ✅ 2026-09-07** (backend `d9437f5` + release `0.72.0`: exhibit builders move to `lenses/{exhibit_model,exhibit_lenses,exhibits}.py` so `build_exhibit` is one source for the hub verdict strips, Copilot `research.exhibit.get` and the brief; `engines/brief/cards.py` builds every card from its exhibit (registry band / label / means, readings, as_of, track record, hub `page_route`), brief gains vrp / skew / term_slope / opex cards + top-level `lenses`; verdict narrative from the terrain exhibit, risk walks event → put-wall → skew hot → backwardation → IV extreme with hub links; lamps gray / green (as of the selected date) / yellow; forecast_path exhibit averages |close miss| (the signed mean showed 0.7% where the Sessions hub says 2.0%). Frontend `09463d1`: `DailyBriefPage` renders the synth only (client-side fallback rules removed), `BriefLensCard` shows the hub's band tag + means + track record + reading-specific caveat + as-of, 'Open' carries symbol + date into the hub view; GEX strip shows the daily zero-γ / walls the verdict rests on beside the intraday snapshot. Acceptance on `:5173` + DEV NVDA 2026-09-04: brief GEX card and Dealer Levels strip both read positive gamma, zero-γ 246, walls 200 / 250, RV20 44.8% vs IV30 33.7%; VRP card and hub both read 21st pctl, IV30 33.7% vs RV60 40.6%; Forecast card and Sessions strip both read 30d path hit 0% across 15, avg |miss| 2.0%) |
| D | Copilot in the loop, and the leash | **D1 ✅ 2026-09-07** (backend `0d68e70` + release `0.73.0`: MCP read tools `research.loop.list_runs` / `get_run` / `explain_candidate` over `copilot/harness/run_digest.py` — a run folded into one object (plan provenance, funnel, hit-rate gate, every candidate with evidence and each judge's stance per model, report why / price / settled / wrong_if, drafts; explain adds the candidate row, hypothesis, settled validation, `what_would_unmake_it`), nothing re-derived from live data; write tool `research.loop.run_objective` (dry_run preview; execution needs the Owner's approval token, refuses a batch pass; shares `batch_orchestrate.start_async_batch` with the HTTP batch-run); loop_curator explain flow, triage routes Loop questions, verdict gets `loop_specialist`. Frontend `40beb44`: per-candidate Ask button in the Inbox batch → Copilot with run + symbol snapshot and an explain prompt; drawer Discuss prompt starts with `get_run`. Acceptance on the local api against DEV data: 'why was RKLB proposed and what would unmake it' on `run_1a078ead64edd0578` → one `explain_candidate` call, answer cites evidence.selection / price_context / track_record, names the gpt-4o-mini portfolio dissent, quotes wrong_if (`path leaves AVOID`, `close breaks below the 50-day (75.24)`), states validate did not block) · **D2 ✅ 2026-09-07** (backend `60523a3` + release `0.74.0`: `copilot/agents/daily_digest.py` posts one `daily_digest` draft per trading day — holdings ∪ candidates proposed since yesterday through `build_exhibit` (iv_rank · vrp · gex_regime · terrain_regime), Loop state (objectives, runs, drafts waiting, trust), candidate batches folded by objective + symbol set, dissents one line per judge model with the report's wrong_if, hypotheses the outcome rule resolved since yesterday; facts by code, DeepSeek rewrites the prose through the planner's endpoint table (≈ $0.001, cost on the payload) and the heuristic markdown stands otherwise; second run the same day is a no-op unless forced; Morning Prep folded in — the digest holds the 11:30 UTC Dagster slot (`research_daily_digest_schedule`); `POST /research/agents/digest/run`. Frontend `856ba67`: `DailyDigestBody` (prose, then the batches folded beneath with Pipeline links), Decision Inbox opens on Briefings with the digest first when one is pending, Copilot `InboxBanner` sorts the digest first and reads 'Daily digest · N more pending'. DEV acceptance: digest `drf_1a07987057bfdfaa074` for 2026-09-07 (11 symbols, 2 batches, 11 dissents, 0 resolutions, deepseek-chat prose 333 words); second post skipped; Inbox on `:5173` opened on it with the two batches folded beneath; `research_daily_digest_schedule` RUNNING on DEV Dagster) · D3 / D4 pending |
| D | Copilot reads runs · daily digest · unattended on a leash · verdicts back on pages | pending |

Owner decisions D-RLA-1…4 recorded in the plan. Out of scope: P0 hygiene (DDL apply / image alignment).

---

## Loop Stock-first + Whitebox Pipeline — LS-1…LS-4 — 0.56.2

**Status:** ✅ Package **0.56.2** (2026-08-31). Stock-first harness + pipeline UI; D10 BLOCKED.

| Wave | Deliverable |
|------|-------------|
| LS-1 | `policy_schema.py` · whitelist `universe_mode/layers/option_overlay` · RUNBOOK · seed `--profile stock` · FE `RECOMMENDED_LOOP_POLICY_STOCK` |
| LS-2a–d | `copilot/harness/universe/*` · runtime routes stock_composite · trace funnel · readiness · Cron `obj-daily-loop-stock` |
| LS-3 | `GET /research/objective-runs/{id}` · FE `/research/loop/runs/:runId` · Harness/LoopBanner/Inbox links |
| LS-4 | Copilot prefill funnel · plan_llm stock-first · loop_curator instructions |
| Spine | `D-Loop-StockFirst-1` … `D-Loop-StockFirst-4` (Ops register) |

---

## Loop Orchestrator — Waves LO-0…LO-5 — 0.55.0

**Status:** ✅ Package **0.55.0** (2026-08-31). Mode 3 batch pipeline; D10 BLOCKED.

| Wave | Deliverable |
|------|-------------|
| LO-0 | `seed_loop_objective.py` · Cron unsuspend DEV · `--require-scan-fresh` · RUNBOOK |
| LO-1 | `copilot/curator/*` headless CuratorRun · `POST .../curate` · FE Run Curator |
| LO-2 | `research.loop.propose_order_intent` MCP · loop_curator instructions |
| LO-3 | `validate_hook.py` stock-leg backtest · approve-all auto_validate · FE Evidence tag |
| LO-4 | `research-loop-batch` Trust skill · `--batch-mode` · L0 gate · whitelist auto-accept |
| LO-5 | market-data option backfill Cron stub (Owner Polygon tier decision) |
| Spine | `D-Loop-Orchestrator-0` … `D-Loop-Orchestrator-4` · `D-Market-Option-History` |

---

## Loop Smartness 下一刀 — 0.48.4

**Status:** ✅ Package **0.48.4** (2026-08-29). Five live-path closures; no new tables/RPCs; D10 BLOCKED.

| Cut | Deliverable |
|-----|-------------|
| Deploy | Local `research-api` loads workspace source (restart; was stale 0.47.0) |
| Approve-all | Reuses Inbox `apply_draft_approval` (policy merge + candidate promote) |
| candidate_batch | Approve → lightweight hypothesis + `promote_candidate`; skip missing/non-open |
| preset | `top_scan_symbols` applies Scan `resolve_preset` / `recompute_composite` |
| lens map | `FLAG_TO_DECAY_LENS`; unmapped / no-decay skipped, not failing |
| Spine | `D-Loop-Cleanup` addendum (0.48.4) |

---

## Research Loop Maturity — Waves Z / R / C / A / O — 0.47.0

**Status:** ✅ Package **0.47.0** (2026-08-29). Ships Loop IA + candidate pool + Copilot write tools + harness (propose-only) + order_intent advisory in one package.

| Wave | Deliverable |
|------|-------------|
| Z+R | IA regroup · `research.candidate_pool` · Loop sidebar (Candidate / Hypothesis / Decision) |
| C | `research.loop.*` MCP write tools · `loop_curator` persona · DiffApproval kinds (`candidate_batch` / `decision_draft` / …) |
| A | `research.objective` / `objective_run` · harness runtime (plan → propose → await_approval) · Cron stub `research-harness` (`suspend: true`) |
| O | `OrderIntent` schema · `order_intent` advisory drafts · Trade Opportunities observe surface |
| Spine | `D-Loop-v1` · `D-Loop-Copilot` · `D-Research-Harness` — D10 remains **BLOCKED** |

---

## Analyze Waves L / M — Decay depth + Alerts — 0.43.0

**Status:** ✅ Package **0.43.0** (2026-08-29). Wave L Signal Decay deepen + Wave M alert bridge.

| Wave | Deliverable |
|------|-------------|
| L.1–L.4 | `/research/signal-decay?regime=` + `recent_triggers`; `/intersect`; FE `:symbol` + 2×2 matrix |
| L.5 | AskCopilot snapshot enrichment (Scan + Signal Decay) |
| M.1–M.4 | `stock_signal_alert_daily` (26 tables) + alert_scan Cron + `/research/alerts` + AlertBell |
| Spine | `D-Analyze-L` · `D-Analyze-M` |

---

## Analyze Wave J / K — Integrity + Portfolio — 0.41.0

**Status:** ✅ Wave J package **0.41.0** + Wave K FE (2026-08-29).

| Wave | Deliverable |
|------|-------------|
| J.1 | K8s YAML image tags aligned (volatility/engines/intraday/…/api/mcp); cluster apply |
| J.2–J.3 | `OPEX_PIN_HOT_ABS=0.010`; lens_hit clear+252d rebuild (1868 rows; opex hot 51 @ 61.5%) |
| J.4 | `_side_stats.pending_5d/20d` + FE pending caption |
| J.5 | playbook_trigger still 4/SPY (weekend — observe Mon EOD; emitter=`engines/forecast/playbook.py`) |
| K | `usePortfolioSymbols` + `PortfolioTag` + Universe chip on 6 Analyze labs |
| Spine | `D-Analyze-J` · `D-Analyze-K` |

---

## Analyze Wave I — Signal Decay — 0.40.0

**Status:** ✅ Package **0.40.0** (2026-08-29). `features.stock_signal_lens_hit_daily` + Cron + `/research/signal-decay` + FE Validate page + Scan `adaptive_30d`.

| Item | Detail |
|------|--------|
| Schema | `stock_signal_lens_hit_daily` — CANONICAL_FEATURE_TABLES=25 |
| Engine | IV Rank / VRP / OpEx Pin triggers + T+5/T+20 side-aware hit |
| API | `GET /research/signal-decay` · Scan `preset=adaptive_30d` |
| FE | `/research/signal-decay` under Validate · Scan Adaptive-30d preset |
| Spine | `D-Analyze-I` |

---

## Analyze Wave G / H — Deploy fill + Scan desk — 0.39.0

**Status:** ✅ Wave G DEV deploy + Wave H Scan desk (package **0.39.0**, 2026-08-29).

| Wave | Deliverable |
|------|-------------|
| G.1–G.4 | Image 0.38.0 push; CronJobs + research-api roll; scan 60d backfill (60 dates / 1680 rows); delete bifrost-analytics-daily; spine `D-Analyze-G` |
| H.1–H.2 | `/research/scan?preset=` + per-lens flag query AND matrix |
| H.3–H.6 | ScanPage URL filters · preset weights · SimilarRegime lens picker · holding/watchlist DenseTag |
| Spine | `D-Analyze-H` |

---

# bifrost-research PROGRESS

## Analyze Waves D / E / F — Scan + Playbook Live + Lens/Overlay — 0.38.0

**Status:** ✅ Code complete (package **0.38.0**, FE + BE, 2026-08-29). DEV DDL/image roll Agent-owned; STG/PROD Owner runbook.

| Wave | Deliverable |
|------|-------------|
| D.1–D.4 | `features.stock_signal_scan_daily` + scan engine/Cron/Dagster + `GET /research/scan` |
| D.5 | FE `/research/scan` Dense table + universe filters + lab jump links |
| D.6 | Signal Health `scan` freshness; spine `D-Analyze-D` |
| E.3 | `GET /research/playbook/hit-rate` + FE Playbook KPI card |
| E.4 | `docs/PLAYBOOK_TRIGGER_ROLLOUT_RUNBOOK.md`; spine `D-Playbook-Live` PENDING_STG |
| F.1 | SimilarRegime lenses `gex_notional` + `regime` |
| F.2 | SimilarRegimeCard on GEX Intraday + Analysis Model |
| F.3 | ForecastPathOverlay + settlement `hourly_realized` |
| Spine | `D-Analyze-DEF` |
| Registry | `CANONICAL_FEATURE_TABLES` → **24** |

---

## Analyze Waves A / B / C — Consistency + Depth + Verification — 0.36.0

**Status:** ✅ Program complete (package **0.36.0**, FE + BE, 2026-08-29)

| Wave | Deliverable |
|------|-------------|
| A | SaveAsHypothesis on all 9 Analyze pages; decision-oriented verdicts |
| B.1 | Vol Surface 2D smile + residual scatter |
| B.2 | SimilarRegime lenses `term_slope` + `pin_distance`; cards on Vol Surface / OpEx |
| B.3 | IV Radar 90d rank sparklines (gauge grid) |
| B.4 | VRP Lab IV–RV time series (already present; retained) |
| B.5 | OpEx Vanna/Charm map + last-3-OpEx-weeks side-by-side |
| B.6 | Analysis Model terrain score sparks via `/forecast/terrain/history` |
| C.1 | Forecast 30d hit-rate from `stock_backtest_settlement` (no duplicate realized table) |
| C.2 | `features.stock_signal_playbook_trigger_intraday` + timeline UI |
| C.3 | Regime-transition counts on Analysis Model |
| Health | freshness: `playbook_trigger` + `forecast_settlement` |
| Spine | `D-Analyze-ABC` in ops-context.yaml |
| Registry | `CANONICAL_FEATURE_TABLES` → **23** |

---

## IDS Waves 1–6 — Historical IV Solver — 0.35.0

**Status:** ✅ Program complete (package **0.35.0**, 2026-08-28)

Dual-source write into `features.option_iv_reconstructed_daily`:

1. **`vendor_snapshot`** — project Polygon IV from `raw_market.option_snapshot` (depth path; primary history)
2. **`ok` / `no_convergence` / `insufficient_inputs`** — Brent BS inversion from `option_daily` OHLCV

Downstream ATM / VRP / canonical PnL prefer reconstructed rows, then fall back to live snapshot (`docs/IV_SOLVER_SPEC.md`).
Canonical PnL cohort uses ~30 DTE ATM + LOCF fill (max 14d) and skips hopeless entries so coverage reflects usable marks.

| Wave | Focus | Gate / deliverable |
|------|-------|--------------------|
| IDS-1 | Spec + schema | `option_iv_reconstructed_daily` DDL + solver contract |
| IDS-2 | Unit + back-check | Round-trip error &lt; 1e-3; vs Polygon ATM median abs rel error &lt; 3% |
| IDS-3 | Cohort coverage | distinct dates ≥ **200**, symbols ≥ **25** |
| IDS-4 | Downstream consume | unified ATM fetch; `canonical_pnl.insufficient_pct` &lt; **0.15** |
| IDS-5 | Governance | Signal Health `iv_reconstruction` block; FE card; image tag **0.35.0** |
| IDS-6 | Chart polish | GexStrikeChart Zero γ / Call Wall / Put Wall; Terrain 5-day regime chips |

**Measured metrics** (local Golden Source, 2026-08-28):

| Metric | Actual | Target |
|--------|--------|--------|
| `iv_reconstruction.rows` | **416589** | — |
| `iv_reconstruction.symbols` | **25** | ≥ 25 |
| `iv_reconstruction.distinct_dates` | **222** | ≥ 200 |
| `iv_reconstruction.solver_ok_pct` | **1.0** (`vendor_snapshot`+`ok`) | — |
| `atm_iv` distinct dates | **200** | ≥ 200 |
| `vrp` distinct dates | **234** | — |
| `canonical_pnl.insufficient_pct` | **0.0** (56030 `iv_interpolated`) | &lt; 0.15 |

---

## Wave Canonical-PnL Foundation (plan Wave 12) — 0.31.0

**Status:** ✅ code complete (Owner schema/pricing review before full cohort backfill)

| Phase | Status | Notes |
|-------|--------|-------|
| W12-P1 DDL + dbt shell | ✅ | `features.stock_signal_canonical_pnl_daily` + `dw_stock.mart_canonical_pnl_daily` |
| W12-P2 Pricing library | ✅ | `engines/backtest/canonical_pnl.py` — 8 unit tests |
| W12-P3 Cohort / coverage | ✅ | `engines/canonical_pnl/compute.py` + `coverage_report` |
| W12-P4 CronJob + API + Dagster stub | ✅ | `cronjob-canonical-pnl.yaml`, `/research/canonical-pnl/*`, Dagster asset stub |
| W12-P5 PROGRESS + skill + Signal Health FE | ✅ | skill + FE `/research/signal-health` |

**Version note:** package was already `0.30.0` at start → Wave 12 shipped as **0.31.0**.

---

## Wave 13 — Watchlist Hypothesis Loop — 0.32.0

**Status:** ✅

- `origin_ref.watchlist_contract_key` + `trajectory_summary` contract
- `POST /research/hypothesis/{id}/refresh-trajectory`
- FE: `WatchlistHypothesisDetail`, `PromoteToWatchlistButton`, `withWatchlistContractKey` on Save-as-Hypothesis
- Stock Watchlist inspector hosts hypothesis journal + canonical PnL spark

---

## Wave 14 — Similar Regime + Signal Health v2 — 0.33.0

**Status:** ✅

- `GET /research/similar-regime` (`vrp` | `iv_rank` only)
- `GET /research/signal-health` (freshness + hypothesis counts + canonical coverage)
- FE: `SimilarRegimeCard`, Signal Health page trust/coverage table

---

## Wave 15 — Copilot Exhibit Contract — 0.34.0

**Status:** ✅ (superseded package **0.35.0** after IDS-5)

- `AnalyzeExhibit` + `GET /research/exhibit/{lens}` + `/composite`
- MCP `research.exhibit.get` + portfolio agent verdict instructions
- FE: Verdict strip / auto-insight on VRP · IV Radar · Terrain · Order Sentiment

---

## Wave 16 — Chart Standards (FE)

**Status:** ✅ baseline

- `bifrost-trade-frontend/docs/CHART_STANDARDS.md`
- VRP: IV/RV dual histogram on VRP Lab
- IV Radar: `IvRankStrip` (0–100 rail; 90d spark when history available)
- GEX / Vol Surface / Terrain: documented targets; existing charts retained

### Wave 16 remaining-migration recommendation (2026-08-28, Signal-Health–driven)

Data-trust snapshot after Waves 12–17 backfill:

| Signal | Rows | Distinct dates | Freshness |
|---|---:|---:|---|
| `stock_signal_vrp_daily` | 96 | **4** | fresh |
| `option_metric_atm_iv_daily` | 13,058 | **84** (sparse Jun-2025→Aug-2026) | fresh |
| `option_metric_iv_percentile_daily` | 1,473 | ~90 | fresh |
| `option_metric_gex_daily` | 540,593 | many | fresh |
| `stock_signal_canonical_pnl_daily` | 107,680 | 69 entry-dates × 26 syms | fresh (64% `insufficient_chain`) |

**Recommendation — pause net-new chart work; do a "polish batch" only:**

1. **DO NOW (quick, high-trust data):**
 - GEX Intraday: label `zeroGamma`, `callWall`, `putWall` lines on `GexStrikeChart` (small refactor, no new component)
 - Terrain: add compact 5-day regime chip strip on `AnalysisModelPage` (existing `mart_terrain_daily` already provides sequence)

2. **WAIT (data history bottleneck):**
 - IV Rank 90d spark on `IvRankStrip` — blocked on `/iv-percentile/history` endpoint + longer `option_metric_iv_percentile_daily` history
 - VRP percentile distribution overlay — meaningful only after `stock_signal_vrp_daily` reaches ≥60 dates
 - Vol Surface 2D heatmap — deferred until Owner asks (existing 3D + term structure is adequate for observe-only)

3. **HOLD (already documented as low-priority):**
 - Order Sentiment heavy chart — table-first per `CHART_STANDARDS.md`
 - Multi-leg Flow chart — table-first per doc
 - Intraday Playbook chart — low priority until Signal Health trust improves

**Trigger for next chart wave:** `signal-health` overall stays `ok` for 30 consecutive days. IDS-4 cleared `canonical_pnl.insufficient_pct` to **0.0** via dual-source IV solver + LOCF (no Plugin-side Polygon history API).

---

## Wave 17 — Verdict Strip + Composite Ribbon (FE)

**Status:** ✅ batch rollout

Shared: `AnalyzeVerdictStrip`, `CompositeRegimeRibbon`, `CopilotAutoInsightChip`

| Batch | Pages |
|-------|-------|
| 1 | VRP Lab, IV Radar |
| 2 | Vol Surface, Terrain (Analysis Model), GEX |
| 3 | Order Sentiment (+ multi-leg), OpEx |
| 4 | Forecast Sessions, Intraday Playbook |

---

## Independent — Dagster prod unblock

**Status:** ⏸ deferred (does **not** block Waves 12–17)

CronJob path is live for canonical PnL / engines. Prod Dagster still needs:

1. Instance storage (Postgres/SQLite)
2. Image `[orchestration]` extra
3. `dbt parse` manifest bake
4. Secrets / PG credentials
5. `k8s/orchestration/dagster.yaml` replicas 0 → 1

Owner: Ops + Infra (`bifrost-platform` + `bifrost-trade-infra`) when ready to migrate refresh off CronJob.
