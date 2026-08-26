# Capability Matrix — Research UX Wave R1–R8 (2026-08-25)

Supplements the Wave 6 parity matrix below. **bifrost-research 0.16.0** · Research Cockpit **Wave RS-E Complete** (E1–E4) + Event-Driven Backtest (RS-C).

## Wave delivery status

| Wave | Scope | Engine | API | FE | Notes |
|------|-------|--------|-----|-----|-------|
| R0 | Matrix baseline | — | — | ✅ | FE + this doc |
| R1 | Nav + Daily Brief landing | — | — | ✅ | `navConfig` 6 groups; default `/research/daily-brief` |
| R2 | FE quick wins | — | — | ✅ | Structures cards, Backtest, Momentum legend, Multi-leg Top-N |
| R3 | Event board 3 acts | — | ✅ themes/macro | ✅ | Theme split, narrative timeline, macro cards |
| R4 | Engine depth | ✅ | ✅ macro/settlement | ✅ | `macro_event_daily`, `EventTagger`, settlement `stats_json`, hourly session rows |
| R5 | Tape + GEX layers | ✅ flow fix | — | ✅ | `raw_market.option_trades`; intraday `volume_net_gex` in chart |
| R6 | Optional sources | partial | — | partial | Manual news; no Schwab; session LLM cost on Forecast; markdown theme registry |
| R7 | Layer A Quick Wins | — | — | ✅ | FE-only: Verdict synth (D-R7-a), URL context, empty hints; replay slider removed (D-R7-b) |
| R8 | Layer B structural | ✅ brief/regime | ✅ synth + regime-stats | ✅ | `GET /daily-brief/synth`; Session Timeline; regime hit meta; ResearchContextBar; L1 CronJob trigger |
| RS-A | Research workflow skeleton | ✅ `research.hypothesis` DDL | ✅ `/research/hypothesis/*` CRUD | ✅ Home + Save-as button + sidebar restructure | Wave RS-A locks D-RS-a/b/c: hypothesis on Golden Source `research` schema, sidebar replaces legacy grouping with stage-based taxonomy (Home / Discover / Analyze / Validate / Data), `/research` becomes Research Home |
| RS-B-VRP | IV-RV Spread lab | ✅ `engines/vrp/` — annualized close-to-close RV (20d/60d/252d), ATM IV 30d, VRP percentile 252d; CronJob `cronjob-vrp.yaml` (10 23 * * MON-FRI) | ✅ `/research/vrp/{latest,history,extremes}` (bifrost-research 0.10.0) | ✅ `/research/vrp-lab` page — Verdict Strip · IV vs RV time series · percentile distribution · High/Low extremes table | New DDL `features.stock_signal_vrp_daily`; `fwd_ret_20d` NULL on initial run (backfill helper exposed) |
| RS-B-Surface | Vol Surface (SVI) lab | ✅ `engines/vol_surface/` — Gatheral raw SVI fit per (symbol, trade_date, expiry), pure-Python Nelder-Mead + optional scipy.least_squares, DTE 7-90 & n≥10; CronJob `cronjob-vol-surface.yaml` (20 23 * * MON-FRI) | ✅ `/research/vol-surface/{fit,term-structure,residuals,skew-extremes}` (bifrost-research 0.11.0) | ✅ `/research/vol-surface-lab` page — Verdict Strip · Term Structure line · Residual heatmap (residual_z / IV toggle · expiry segment) · Skew Extremes table | New DDL `features.option_surface_fit_daily` + `features.option_surface_residual_daily`; Gatheral arb-free bound checked; RMSE < 0.01 on smooth synthetic smiles |
| RS-B-OpEx | Vanna/Charm/OpEx cycle lab | ✅ `engines/opex_cycle/` — analytical BS Vanna+Charm (r=q=0), dealer-oriented aggregation (customer-long-call / customer-short-put), zero-crossing strikes; US monthly OpEx = third Friday via `calendar.next_opex_friday`; DTE 7–90 window; CronJob `cronjob-opex-cycle.yaml` (30 23 * * MON-FRI) | ✅ `/research/opex-cycle/{current,history,pin-analysis}` (bifrost-research 0.12.0) | ✅ `/research/opex-cycle-lab` page — Verdict Strip · Vanna/Charm map (dual bars per strike + spot / zero-strike guides) · 12-cycle timeline · Pin-Risk table + pin-rate | New DDL `features.option_metric_vanna_charm_daily` (PK symbol,trade_date); vanna/charm match numerical difference quotients (rel_tol 5e-3, abs 1e-3 on ATM/OTM); `next_opex_friday(2026-08-25) → 2026-09-18`; pin bands 0.5% / 1.5% |
| RS-C1 | Event-driven backtest query engine | ✅ `engines/backtest/event_defs.py` + `strategy_templates.py` (6 templates) + `event_query.py` — resolves `earnings` / `opex` / `sepa_hit` / `iv_percentile_threshold`; `sql` raises NotImplementedError (v1); MFE/MAE + summary metrics | — (pure compute) | — | Earnings source: `raw_market.corporate_action` → `features.event_signal_radar_daily` heuristic → hard-coded stub (9 symbols) — logged in `event_source` |
| RS-C2 | Realistic fill model | ✅ `engines/backtest/fills.py` — `FillConfig(slippage_pct_of_spread=0.2, commission_per_contract=0.65)`; `compute_fill_price` mid ± slippage; degrades to close when bid/ask missing; commissions applied per side | — | — | `settlement.py` left untouched (deals with forecast, not option fills); fill layer applied inside `event_query.py` |
| RS-C3 | Walk-forward + benchmark | ✅ `engines/backtest/walk_forward.py` (build_windows / run_walk_forward / aggregate_oos) + `benchmark.py` (spy_buy_hold_metrics / zero_signal_control) | — | — | v1 uses P&L proxy series from event runs; underlying-price WF is future work |
| RS-C4 | Event Query API + Backtest FE + Hypothesis linkage | ✅ `research.backtest_run` DDL + `repositories/backtest_run.py` | ✅ `POST /research/backtest/event-query` · `GET /research/backtest/runs` · `GET /research/backtest/run/{run_id}` (bifrost-research 0.13.0) | ✅ `/research/backtest` gains Settlement · Event Query tabs; `EventQueryBuilder` + `BacktestRunResultCard`; ResearchHome Recent Backtests wired | Auto-appends `run_id` to `research.hypothesis.linked_backtest_ids` when `hypothesis_id` provided |
| RS-E1 | Static Research Cockpit (no AI) | — | — | ✅ Drawer `⌘K` · Pins · Context · Actions · Freshness lamps | D-RS-E-a/b implemented |
| RS-E2 | Read-only AI Copilot + Research MCP | ✅ MCP 25 tools (`bifrost_research.mcp`) | ✅ `POST /research/copilot/stream` · `GET /research/copilot/usage` · MCP `:8796/sse` | ✅ Copilot tab enabled · Settings AI Usage tile | D-RS-E-c/d/h/i; bifrost-research **0.14.0** |
| RS-E3 | Morning / EOD Agents + Approval Inbox | ✅ `copilot/agents/` heuristic+LLM · CronJobs `k8s/agents/` | ✅ `POST /research/agents/{morning,eod}/run` · `GET/POST /research/drafts*` · DDL `ai_action_log` + `ai_draft` | ✅ Cockpit Inbox tab + Approve/Dismiss · Actions on-demand run | D-RS-E-e/f/g; agents **draft-only**; bifrost-research **0.15.0** |
| RS-E4 | Interactive AI write actions | ✅ Write MCP tools (`create`/`patch`/`retire`/`run_event_query`) + HMAC approvals | ✅ `POST /research/copilot/{approve,execute,dismiss}` | ✅ `DiffApprovalCard` · `DiffPayloadRenderer` in Copilot | D-RS-E-e/g; dry_run default; bifrost-research **0.16.0** · runbook `docs/COCKPIT_RUNBOOK.md` · **program complete** |

## Owner decisions (defaults)

See `docs/RESEARCH_UX_DECISIONS.md`.

## Research pages (21 FE routes)

| Route | Page | Primary API | FE |
|-------|------|-------------|-----|
| `/research/daily-brief` | Daily Brief | multi | F |
| `/research/event-radar` | Event Radar | event-radar + macro | F |
| `/research/intraday-playbook` | Intraday Playbook | terrain/intraday | F |
| `/research/forecast-sessions` | Forecast Sessions | forecast/sessions | F |
| `/research/gex-intraday` | GEX Intraday | gex/intraday | F |
| `/research/analysis-model` | Analysis Model | terrain + smile | F |
| `/research/momentum-radar` | Momentum Radar | momentum/radar | F |
| `/research/sepa-daily-core` | SEPA Daily Core | sepa/model/* | F |
| `/research/sepa` | Stock Screener | analytics/sepa | F |
| `/research/discovery` | Option Discovery | market + research | F |
| `/research/iv-radar` | IV Radar | iv-percentile | F |
| `/research/vrp-lab` | IV-RV Spread (VRP) Lab | vrp/{latest,history,extremes} | F |
| `/research/vol-surface-lab` | Vol Surface (SVI) Lab | vol-surface/{fit,term-structure,residuals,skew-extremes} | F |
| `/research/opex-cycle-lab` | OpEx Cycle Lab | opex-cycle/{current,history,pin-analysis} | F |
| `/research/order-sentiment` | Order Sentiment | flow/* | F |
| `/research/watchlist` | Watchlist | trade API | F |
| `/research/screener` | Option Screener | legacy | F |
| `/settings/data-readiness` | Stock Data Readiness | plugin readiness | F |
| `/research/greeks` | Contract Greeks | research API | F |
| `/research/risk` | Risk Model | monitor | F |
| `/research/backtest` | Backtest (Settlement · Event Query tabs) | backtest/settlement · backtest/event-query | F |

**Frontend Wave R**: 21/21 routes wired (18 + Wave RS-B: VRP + Vol Surface + OpEx Cycle labs).

---

# Capability Matrix — Product Parity (Wave 6)

Analysis Case 1/2 截图 + 事件雷达工作流 → 引擎 / API / 前端三层对标。

## 状态标记

- **E** = Engine 已实现
- **A** = API 端点已暴露
- **F** = Frontend 页面已有
- **--** = 缺失

---

## Case 1: SPX 分析 + 雷达

| # | 截图 | 业务能力 | Engine | API | Frontend | 目标 Phase | 验收 |
|---|------|---------|--------|-----|----------|-----------|------|
| C1-1 | 2319-2320 | SPX 日内剧本 — 四场景概率条 (Rangy/Bull/Bear/Squeeze) | E (playbook.py ScenarioProbabilities) | A (POST /forecast/sessions/compute) | F (IntradayPlaybook) | R6 | 概率条 UI 显示四段 + 百分比 |
| C1-2 | 2319-2320 | 四场景卡片 — 关键位 / 策略建议 / 止损 / LIVE 状态 | E (playbook.py HourlyPathCall + OptionStructureRec) | A (GET /forecast/sessions/{id}) | F (IntradayPlaybook) | R2 | 四张场景卡各显示关键位 + 建议文本 |
| C1-3 | 2321 | 价格扇形图 — 预测带 + 实际 spot 叠加 + 路径转换日志 | E (terrain intraday) | A (GET /terrain/intraday) | F + replay slider | R6 | SVG 扇面 + spot 线 + 转换表 |
| C1-4 | 2322 | IV Smile — OTM 散点 + 拟合曲线 + R² + ATM IV | E (surface.py smile_params) | A (GET /volatility/smile) | F (Discovery 近似) | 6.3.1 | 双面板: 散点+拟合 / 拟合分析 |
| C1-5 | 2323 | SPX 分析模型 — terrain 四维评分 + regime + close 预期 | E (terrain.py MarketTerrain) | A (GET /forecast/terrain) | F (AnalysisModel 3-col) | R6 | 三列 Card: 地形 / 预期 / IV 曲面 |
| C1-6 | 2323 | 收盘预期 — 预期/远期价格 + 置信区间 + 盘前机制 | E (terrain expected_close + gamma_zone) | A (GET /forecast/terrain) | F | R6 | 中间 Card 数值面板 |
| C1-7 | 2324-2325 | 动能雷达 — 多股得分 grid + grade + path + 三时间帧 | E (momentum/radar.py) | A (GET /momentum/radar) | F | R2 | 6×N 卡片 grid + grade 颜色 + 筛选 |
| C1-8 | 2325 | 动能因子解读 — 因子含义 / 何时好 / 何时差 | E (factors_json) | A (同上) | F (legend) | R2 | 页面底部折叠解读面板 |
| C1-9 | 2326 | IV 雷达 — 三区域分桶 (高/中/低速动率) + 股票计数 | E (iv_percentile_daily) | A (GET /analytics/options/iv-percentile) | F (IvRadarPage) | 6.3.4 | 增强: 三区域 Card 显示数量 |
| C1-10 | 2327 | IV Gauge — 单股 circular gauge (IV Rank 0-100) + ATM IV + IV Pctl | E (iv_percentile_daily) | A (同上) | -- | 6.3.4 | 新增 Gauge Grid 视图 + IvGauge 组件 |
| C1-11 | 2328 | 大单 / 多腿追踪 — 策略分类 + 到期/行权价/Greeks | E (multi_leg_trades) | A (GET /flow/multi-leg) | F | R2/R5 | DenseDataTable + top 大单 grid |

## Case 2: GEX + Forecast + Settlement

| # | 截图 | 业务能力 | Engine | API | Frontend | 目标 Phase | 验收 |
|---|------|---------|--------|-----|----------|-----------|------|
| C2-1 | 3652 | GEX 日内走势 — GEX by volume + spot overlay + key levels | E (gex intraday) | A (GET /gex/intraday) | F | R5 | SVG 图: 柱状 GEX + spot 线 + 水平线 |
| C2-2 | 3653 | GEX 横向 bars — Pos/Neg by strike + spot trace + 多时间帧 | E (levels_json OI+Vol) | A (同上) | F (dual layer) | R5 | 水平柱状图 + spot 轨迹 + 时间帧切换 |
| C2-3 | 3654-3655 | Forecast Run 列表 — Daily/Hourly/Weekly + Input Snapshot + Evidence | E (playbook.py ForecastSession) | A (GET /forecast/sessions) | F | R2 | 左侧 Run sidebar + 右侧 detail |
| C2-4 | 3655 | Evidence Summary — Total/Flow Score/Flow Bias/Proxy/Market Bias + 标的 | E (narrative + terrain_json) | A (GET /forecast/sessions/{id}) | F + LLM cost | R6 | Input/Evidence Card |
| C2-5 | 3657-3658 | Forecast Window Detail — Path Call + Expected Levels + Option Structure | E (HourlyPathCall + OptionStructureRec) | A (GET /forecast/hourly) | F | R2 | DenseDataTable + structure cards |
| C2-6 | 3678-3683 | Settlement — Actual + Forecast Settlement (Direction/Path/Close) | E (settlement.py) | A (GET /backtest/settlement) | F (Backtest) | R2/R4 | Settlement 列 + badges |
| C2-7 | 3714-3715 | Settlement badges — Direction hit / Path shape / Close zone / miss tags | E (stats_json) | A (同上) | F | R3/R4 | DenseTag success/danger badges |
| C2-8 | 3660 | Order Sentiment — 全日名义金额 + 情绪条 + 到期/行权价集中度 | E (order_sentiment_daily) | A (GET /flow/sentiment) | F | 6.4.3 | KPI 条 + 渐变情绪条 + 柱状图 |
| C2-9 | 3661 | Multi-leg 大单表 — 成交/合约/策略/VWAP/BID/ASK/DTE | E (multi_leg + tape) | A (GET /flow/multi-leg) | F | R2/R5 | DenseDataTable + 分页 |

## Event Radar Workflow

| # | 步骤 | 业务能力 | Engine | API | Frontend | 目标 Phase | 验收 |
|---|------|---------|--------|-----|----------|-----------|------|
| ER-1 | 01_拆解 | 原始文本 → events_raw (parse) | E (pipeline.py step_parse) | A (POST /event-radar/run) | -- | 6.5 | API 接收文本返回 PipelineResult |
| ER-2 | 02_清洗 | 去重 + 噪声过滤 | E (pipeline.py step_clean) | A (同上) | -- | 6.5 | kept_count + dropped_count 正确 |
| ER-3 | 03_打标 | 方向/确定性/情绪/主线/重要性 | E (EventTagger + pipeline) | A (同上) | -- | R4 | 各维度分布合理 |
| ER-4 | 04_出表 | 结构化导出 (13 列) | E (pipeline.py step_export) | A (GET /event-radar/events) | F | 6.5.2 | DenseDataTable 显示事件表 |
| ER-5 | 05_自检 | 14 项质检 | E (pipeline.py step_self_check) | A (self_check 在 run 返回) | -- | 6.5.2 | 自检结果面板 |
| ER-6 | dashboard | 事件表 + 主线甘特 + 多空撕裂 + 前瞻日程 | E (macro CSV) | A (themes + macro/gap/forward) | F (dashboard) | R3/R4 | 四区域看板 |
| ER-7 | schema | 主线注册表 — 增删改 + 退场 | -- (静态 md) | A (/events/themes) | F (theme split) | R6 | 主线分布面板 |

## 跨域基础设施

| # | 领域 | 能力 | 状态 | 目标 Phase | 说明 |
|---|------|------|------|-----------|------|
| X-1 | DDL | terrain_intraday 表 | E | 6.2.1 | PK: (symbol, trade_date, asof_ts) |
| X-2 | DDL | gex_intraday 表 | E | 6.2.1 | PK: (symbol, trade_date, asof_ts) |
| X-3 | Engine | terrain intraday 计算 | E | 6.2.2 | compute_market_terrain + asof_ts |
| X-4 | Engine | GEX intraday 计算 | E | 6.2.2 | compute_gex + levels_json OI/Vol |
| X-5 | K8s | intraday CronJob | E | 6.2.3 | 每小时 09:30-16:00 ET |
| X-6 | K8s | settlement CronJob | E | 6.6.2 | 每日 17:00 ET |
| X-7 | API | Research Engine API 集成 (FE → :8795) | F | R1+ | src/api/researchEngine.ts |
| X-8 | FE SVG | ScenarioFanChart / GexStrikeChart / ProbabilityBar | F | R2-R6 | 自定义 SVG |

---

## 总计

- 能力条目: 27 条 (Case 1: 11, Case 2: 9, Event Radar: 7)
- 基础设施: 8 条
- Engine 已实现: 24/27 (89%) post-R4/R5
- API 已暴露: 22/27 (81%)
- Frontend 已有: 20+/27 — Wave R 交付后 Research 域页面齐全；IV Gauge grid 仍 backlog
- **Wave R 完成** · backlog: C1-10 IV Gauge, gexbot precision, Console theme registry editor
