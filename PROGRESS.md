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
