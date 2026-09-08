---
version: 2026-09-08.5
updated: 2026-09-08
status: 宇宙规则已落地 · 核枚举爬坡中
---

# Research 校准

> **这是现状，不是目标。** 目标在《Research 蓝图》。这份文档回答"代码今天离蓝图多远、差在哪"，每次校准都会变。
> 契约按蓝图里的编号引用，编号在蓝图里定义，这里只记状态。
> 同一份文件：仓库 `bifrost-research/src/bifrost_research/docs/RESEARCH_CALIBRATION.md`，
> API `GET /research/docs/calibration`，UI `/docs/research-calibration`。

## 0. 怎么用

每次校准：逐条契约答"今天满足吗"，写下证据（文件、行、或一条查询）；不满足的写**最小改动**；然后在 §3 更新差距清单。**排序不在这里定**——先补哪边是讨论出来的，讨论结果记进 §4。

状态：✅ 满足　⚠️ 部分　❌ 不满足　⏳ 代码已到位等数据

## 1. 现状数字（2026-09-08 快照）

### 1.1 宽

| 分析面 | 覆盖标的 | 数据截止 |
|---|---|---|
| SEPA（股票） | 3,475 | 2026-09-05 |
| terrain / 期权扫描 | 28 | 09-07 / 09-04 |
| momentum / VRP / GEX | 27 | 09-04 |
| IV percentile | 26 | 09-04 |
| `raw_market.option_daily` 最近十天 | **19** | 09-04 |

对照蓝图 §3.1：宇宙没有明确规则；股票面与其余七个面的覆盖差约 130 倍；存在大量"半个标的"。

### 1.2 深

九个 lens 里三个几乎不触发：skew 建表至今 1 行，terrain_regime 4 行，order_sentiment 0 行。对照蓝图 C-F4。

### 1.3 接缝

今日一次 Smart Decision Run 的漏斗：3,475 → 43（SEPA）→ momentum 返回 0 → events 跳过 → 期权 overlay 跳过（快照过期 4 天）→ 24 → 8。原因不是 bug：momentum 宇宙 25 个标的，与 43 个 SEPA 幸存者交集 5 个，那 5 个里没有 grade A。多层筛选实际是单层。对照蓝图 §3.3 与 C-A2：筛选层把没有深面的标的送进了判断层，判断层对缺面静默出了结论。

### 1.4 三层各自碰到的东西

| 层 | 读的表 / 引擎 | 对照 |
|---|---|---|
| 智囊 harness | 5 张表（sepa、scan、lens_hit、candidate_pool、candidate_outcome），自己的 SQL；只复用回测引擎 | C-A1 ❌ |
| Copilot 工具 | 63 个工具，import engines / lenses；覆盖 VRP、vol surface、opex、GEX、flow、forecast、backtest、playbook | C-C2 ✅ |
| 页面 API | 16 张表 | 基础层的皮 |

"命中率 / 实绩"在代码里 ≥ 10 处实现，回答 4 个问题，`lenses/track_record.fetch_track_record` 与 `harness/evidence._fetch_track_record` 名字几乎相同、问题完全不同。对照 C-R1。

### 1.5 三个不触发的 lens：根因各不相同（C-F4）

| lens | 上游 | 近 30 天上游 | 触发规则 | 为什么不触发 | 性质 |
|---|---|---|---|---|---|
| skew | `option_surface_fit_daily.atm_slope` | 27 标的 | \|slope\| 在自身 252 日百分位的 hot 带，且历史 ≥ `SKEW_MIN_HISTORY_DAYS`=60 | 历史只有 3–10 天（表从 2026-08-21 开始）；今天有 3 个标的百分位 ≥ 90，历史够就会触发 | **成熟度门**，约 60 个交易日后自愈，不用改 |
| terrain_regime | `stock_forecast_terrain_daily.regime` | 28 标的 | 只有 `crash-risk` 算 hot | 近 30 天 range 537 / trending 19 / crash-risk 4；4 行正好对上 | **设计上稀有**，尾部风险标；命中率永远不会有统计意义 |
| order_sentiment | `option_flow_sentiment_daily.sentiment_score` | 28 标的、418 行 | 只认 `data_source == option_trades_tape`，带 hot=+30 / cold=−30 | **418 行全是 `option_snapshot_aggregates`**，tape 源不存在；分数本身很极端（均值 16，大多数在 ±30 外） | **数据源门**：exhibit 层同样拒信 aggregates（`exhibit_lenses.py:486`），口径一致；是否放开是订阅层面的决定 |

### 1.6 覆盖矩阵（近 30 天有数据的标的数）

| 面 | 表 | 标的 | 截止 |
|---|---|---|---|
| 趋势与结构 | `stock_signal_sepa_daily` | **3,475** | 09-05 |
| 趋势与结构 | `stock_signal_momentum_daily` | 27 | 09-04 |
| 波动面 | `option_surface_iv_daily` / `_fit_daily` / `_residual_daily` | 27–28 | 09-04 |
| 波动面 | `option_metric_iv_percentile_daily` / `atm_iv_daily` / `vrp_daily` | 26 | 09-04 |
| 持仓面 | `option_metric_gex_daily` / `gex_levels_daily` / `vanna_charm_daily` | 27 | 09-04 |
| 持仓面 | `option_metric_max_pain_daily` / `pcr_daily` / `flow_sentiment_daily` / `flow_multi_leg_daily` | 28 | 09-04 |
| 持仓面 | `option_metric_gex_intraday` | 19 | **09-02**（落后两个交易日） |
| 预测面 | `stock_forecast_terrain_daily` / `_session` / `_hourly` | 28 | 09-07 |
| 验证面 | `stock_signal_lens_hit_daily` | 28 | 09-04 |
| 验证面 | `stock_backtest_settlement` | 26 | 09-04 |
| 告警 | `stock_signal_alert_daily` | **1** | 09-04 |

对照蓝图 §3.2 的六个面：除趋势与结构里的 SEPA 之外，其余全部落在 26–28。事件面（`event_signal_radar_daily`）按事件而非标的存，不在此表。

## 2. 契约状态

### 基础层（2026-09-08.2 深校准）

| 编号 | 状态 | 证据 |
|---|---|---|
| C-F1 | ❌ | 基础层**没有批量筛选原语**：`lenses/exhibits.build_exhibit(conn, lens, symbol)` 逐标的，registry 只有 `scan_flag(band)` 一个 helper。harness 要在 3,475 个标的上筛，只能自己写 SQL（`copilot/harness/universe/{sepa,momentum,events}.py` 各自 SELECT `features.*` 并重做 score / grade / importance 过滤）。这不是 harness 不守纪律，是基础层缺一个它需要的能力 |
| C-F2 | ✅ | `engines/`、`lenses/` 无 objective / policy 依赖；`iv_solver.py`、`vol_surface/fit.py` 里的 `objective` 是最小二乘的目标函数 |
| C-F3 | ⚠️ | 12 个 spec 都有 route、bands、hot/cold 说明。**0.95.0 起 sepa 与 momentum 加入衰减追踪**（`decay_lens` + `hit_rule=follow`，hot 分别要求 SETUP/PIVOT 与 A 级），最宽的两个面开始自我度量；仍无 decay 的 3 个：iv_percentile、term_slope、forecast_path |
| C-F4 | ❌ | 三个 lens 不触发，上游表都有 27–28 个标的、数据齐全，根因各不相同（见 §1.5） |
| C-F5 | ⏳ | **规则已落地（0.95.0）**：`research.option_universe` 三层，首次填充 575（常驻 27 / 核 527 / 边 21），`load_symbols_from_env_or_query` 优先读它，Dagster asset `engines/option_universe` 每日刷新。**供给爬坡中（Plugin 0.16.0）**：option-refresh 以 `universe: research` 枚举，2026-09-08 18:05 UTC 首跑排入 173 个任务（常驻 11 个优先级 7、核 162 个优先级 6），每次最多 150 个新名字，六小时一次。核全部枚举完约 1–2 天，之后 `option-backfill` 一次性按行回填（边 12 个月、其余 24 个月） |

### 实绩层

| 编号 | 状态 | 证据 |
|---|---|---|
| C-R1 | ❌ | 见 §1.4 |
| C-R2 | ⏳ | 0.91.0 对齐基准腿；`not_elapsed=132` 等 bar；3/3 已判 |
| C-R3 | ✅ | 0.90.0 `decline_memory`；漏斗有 `decline_memory` 步；回填 3 条 |
| C-R4 | ✅ | 0.91.0 `engines/signal_hit_fwd_fill`；一次回填 1,310 行 |
| C-R5 | ✅ | `ai_draft`、`ai_action_log`、`persona_eval_spend` |
| C-R6 | ⚠️ | `rating.settled_record` 与 Copilot 各自取战绩，口径未证明一致 |

### 智囊

| 编号 | 状态 | 证据 |
|---|---|---|
| C-A1 | ❌ | `harness/data_sources.py` 直接 SELECT `features.*` |
| C-A2 | ❌ | 见 §1.3 |
| C-A3 | ⚠️ | 报告有 why / price / settled / wrong_if；但系统以候选数与 `auto_approve_eligible` 自评，新信息率与事后正确率未度量 |
| C-A4 | ✅ | `research.loop.list_runs / get_run / explain_candidate` + daily digest |
| C-A5 | ✅ | D10 守卫；propose-only；绳子四道门 |
| C-A6 | ❌ | 名为 Autopilot |
| C-A7 | ✅ | `rating.py`：conviction / action / timing / levels / outlook / instrument / why 全部存在；战绩来自 `candidate_outcome` |
| C-A8 | ⚠️ | 高级一端就是 Smart Decision Run；基础一端（只摆读数、评级从简）没有独立入口 |

### 操作面

| 编号 | 状态 | 证据 |
|---|---|---|
| C-C1 | ✅ | Inbox、digest、verdict strip 都可进 Copilot |
| C-C2 | ✅ | 63 工具 |
| C-C3 | ✅ | `mcp/tools/*` import engines / lenses |
| C-C4 | ✅ | 审批 token、`fill_tool_defaults`、bearer 解析 owner |
| C-C5 | ✅ | discovery / vrp / vol_surface / backtest 工具不依赖任何 run |

### UI

| 编号 | 状态 | 证据 |
|---|---|---|
| C-U1 | ✅ | `researchNavCatalog.seatForRoute`，21 个测试 |
| C-U2 | ✅ | 0.92.0 `pending_decision_calls`；两侧独立算得 34/19/24 |
| C-U3 | ✅ | Research、Portfolio、Strategy |
| C-U4 | ✅ | Objectives 不再借控制台路由 |

**计数**：✅ 15　⚠️ 5　❌ 6　⏳ 1，共 27 条。（基础层深校准后 C-F1 由 ⚠️ 改 ❌、C-F3 由 ✅ 改 ⚠️、C-F5 补充证据）

## 3. 已知差距与最小改动（未排序）

| 契约 | 差距 | 最小改动 |
|---|---|---|
| C-A1 / C-F1 | 智囊自己读表、自己筛 | harness 改为经 `lenses/` 取读数；删 `harness/universe/*` 里重做的筛选 |
| C-R1 / C-R6 | 实绩十个实现 | 一个 `track_record` 模块，四个问题四个函数，其余全部改为调用 |
| C-A2 | 接缝无契约 | 筛选层校验深面存在；判断层缺面时标注"未测"而非静默 |
| C-A3 | 报告以错误尺子自评 | 度量新信息率与事后正确率，上 Overview |
| C-F4 | 三个 lens 不触发 | 先查阈值还是上游数据（task 已开） |
| C-F5 | 宇宙无规则 | 写下宇宙规则；宽度需求交 Plugin / 订阅 program |
| C-A6 | 命名 | Autopilot → 智囊（英文待定）：seat、路由、存储键、文案 |
| C-A8 | 基础一端缺入口 | 给 objective 一个"只摆读数"的模式 |

## 3b. 基础层：拉近差距的选项（供讨论，未定）

差距只有三类，对应三种性质不同的动作：

**甲 · 缺一个原语（C-F1，连带 C-A1）**
基础层要长出**批量筛选**：对一个宇宙、一组 lens、一组 band/阈值，返回幸存者与每个标的的读数，用的是 registry 里同一份定义。harness 随后改为调用它，删掉自己的 SQL。这是唯一能同时关掉 C-F1 与 C-A1 的改动，也是接缝契约（C-A2）能落地的前提：筛选原语知道每个标的有哪些面，才能拒绝把缺面的标的送进判断。
- 代价：一个新模块 `lenses/screen.py`，加 harness 三个 universe 文件的替换；纯 Research 侧。
- 不做的后果：C-A1 永远无法满足，智囊与页面读数不一致的可能性一直存在。

**乙 · 度量缺口（C-F3、C-R4 相关）**
sepa 与 momentum 两个最宽的面没有衰减追踪。给它们加 `decay_lens`，定义触发（如 SEPA 进入 PIVOT、momentum 升到 A）与命中口径，signal_hit 引擎自动接管回填。
- 代价：registry 两个 spec、`signal_hit/build.py` 两个 classify、一次回填；纯 Research 侧。
- 不做的后果：覆盖 3,475 个标的的那一层永远不知道自己准不准，智囊对它的评级没有战绩可依。

**丙 · 宇宙（C-F5）：折中方案（2026-09-08.3，供拍板）**

Owner 的判断：SEPA 那么宽（3,475）对期权面不现实，今天的 27 又太窄。下面是量出来的账和一个三层规则。

*成本单位（2026-09-08 实测，Plugin 4 个 option worker，付费 Starter 无调用上限）*

| 项 | 单只个股 | 指数 / ETF |
|---|---|---|
| 一次性枚举（`option_expiration` + `option_contract`） | ≈ 7 分钟 | ≈ 16 分钟 |
| 一次性回填，24 个月、±30% 行权价带 | ≈ 10.6 千个任务 ≈ **13 分钟**墙钟 | ≈ 124 千个任务 ≈ 2.6 小时 |
| 每日快照 | ≈ 10 秒 | ≈ 48 秒 |
| 每日新增行 | ≈ 73 行 | ≈ 1,200 行 |

回填速率 48 千任务 / 小时。今天 78 万 pending 是当前 27 个标的的两年历史，其中 72% 来自 5 个指数 / ETF。

*候选规模（最新交易日）*

| 规则 | 标的数 |
|---|---|
| 20 日均美元成交额 ≥ $500M | 285 |
| ≥ $200M | 712 |
| ≥ $100M | 1,198 |
| SEPA SETUP/PIVOT，score ≥ 70（每日幸存者） | 43 |
| score ≥ 60 | 387 |

43 个幸存者里 11 个在 $500M 核、21 个在 $200M 核，5 个今天已有期权数据。今天的 27 个里 20 个在 $500M 核。

*方案：三层宇宙，两条规则，零手工名单*

| 层 | 规则 | 规模 | 历史深度 | 进出 |
|---|---|---|---|---|
| 常驻 | 持仓 ∪ Owner 自选 ∪ {SPY,QQQ,IWM} | ≈ 27 | 24 个月 | 不退出 |
| 稳定核 | `dim_universe` ∩ 可期权 ∩ 20 日均美元成交额 ≥ 阈值 | $500M → ≈ 285；$200M → ≈ 712 | 24 个月 | 每月重估，进入用阈值、退出用阈值的 60%（滞回），保住百分位历史 |
| 轮动边 | 股票筛选的幸存者（SEPA SETUP/PIVOT ≥ 70）中不在前两层的 | 每日新增 ≤ 30，稳态 ≈ 50–150 | **12 个月**（够 IV rank 的 252 日） | 首次出现即进入并回填；最后一次出现后保留 90 个交易日（20 日命中结清 + skew 60 日成熟） |

**接缝契约随之落地**：筛选层只把期权面已入库 ≥ 1 个交易日的名字送进判断；新进入轮动边的名字当天只按股票面判断，卡片写明"期权面：采集中，N 日"。这正是 C-A2 要求的"缺面明说"。

*两档总量与代价*

| 档 | 总标的 | 一次性（4 worker 墙钟） | 每日稳态 |
|---|---|---|---|
| 核 $500M | ≈ 27 + 285 + ~100 ≈ **400** | ≈ 24 小时 | 快照 ≈ 12 分钟；新增 ≈ 3 万行 |
| 核 $200M | ≈ 27 + 712 + ~80 ≈ **820** | ≈ 2.5 天 | 快照 ≈ 30 分钟；新增 ≈ 6 万行 |

对比：今天 27，SEPA 3,475。$500M 档是今天的 15 倍、SEPA 的九分之一，由两条规则而非一份名单决定。

*谁拥有什么*
- 规则与清单：Research。发布为 `research.option_universe`（表或视图：symbol、tier、entered_on、last_seen、history_months）。
- 采集：Plugin。`scheduler/daily.py` 的 `load_watchlist_from_platform` 旁边加一个 `load_universe_from_research`，Plugin 已有"按 Research dim_universe 同一过滤"的先例（`CS_UNIVERSE_QUERY`）。这是 Plugin 改动，归 program `market-data-subscription-focus`，Owner 批。
- 在 Plugin 接上之前，Research 侧先把规则和表建好，`load_symbols_from_env_or_query` 改读它；期权面覆盖不会变，但 C-F5 的"规则"那一半先关掉。

*未量的风险*
- 下游引擎（vol surface fit、GEX、VRP……）按标的跑，400 个标的的耗时**未测**，trading-day job 的时间窗必须装得下。选 $200M 档前必须先量。
- `option_expiration` 的周期未查到；若是每周，$500M 档每周 ≈ 33 worker 小时（4 worker ≈ 8 小时）。
- 分区表（`option_daily` 等）的体积没量到，只量到 `option_contract` 71 MB（27 个标的），线性外推 400 个 ≈ 1 GB。
- `raw_market.option_daily` 只有成交过的合约，中位数每标的每日 23 行；轮动边里流动性差的名字期权面会很稀，lens 会正确地报"未测"。

*建议*：$500M 档先行，跑一个月量下游引擎耗时，再决定要不要放到 $200M。

**三个死 lens（C-F4）不是一类动作**：skew 等时间；terrain_regime 保持稀有，或另议是否把 trending 也算一档；order_sentiment 跟丙走，tape 源来了才活。

## 4. 排序与决定

### 4.1 已定（2026-09-08，Owner）

- **宽度**：采用三层宇宙、两条规则、零手工名单；稳定核用 **$200M**（覆盖优先）。
- **顺序**：乙（sepa / momentum 加衰减追踪）可先做，不依赖甲；甲（批量筛选原语）与丙的规则并行；丙的接缝执行等甲。

### 4.2 修正后的规模（$200M 核限定 `dim_universe` 普通股）

| 层 | 规则 | 今日 |
|---|---|---|
| 常驻 | 今天的 27（自选 ∪ 基准） | 27 |
| 稳定核 | `dim_universe` ∩ 20 日均美元成交额 ≥ $200M | **547**（其中 20 与常驻重合） |
| 轮动边 | SEPA SETUP/PIVOT ≥ 70，且不在前两层 | 21（幸存者 43） |
| **合计** | | **575** |

之前的 712 混入了 ETF 与非普通股。核的门槛边缘是 EFX / LEN / QXO / SYY / THC，日均 $201M，都是正经大盘股，阈值合理。可期权性不用预判：`raw_market.ticker` 无此字段，枚举时无合约的标的自然退出。

**一次性代价（4 个 option worker，实测速率）**：枚举 547 × 7 分钟 ≈ 64 小时；回填 24 个月 547 × 13 分钟 ≈ 118 小时，12 个月约减半。两者可并行，合计约 **5 个自然日**；worker 加到 8 个约 2.5 天（付费 Starter 无调用上限，`rate_per_sec` 是每进程软上限）。

### 4.3 当前队列：不杀，让它跑完

Owner 看到 744,811 个任务，担心永远跑不完。实测（2026-09-08 17:34 UTC）：

| 项 | 值 |
|---|---|
| pending | 770,871（planner 仍在扩展，所以比 Console 上的数略大） |
| 最近三小时消化速率 | 每 15 分钟 1.1–1.5 万，≈ **5 万 / 小时** |
| 预计完成 | ≈ **15 小时** |
| worker 日志 | 干净，无 429 / 403 / 超时 |
| 构成 | SPXW 22.3 万、SPY 14.5 万、QQQ 13.6 万、IWM 7.6 万、SPX 1.8 万 = **78%** 指数类；其余 META / TSLA / MSFT / NVDA / DDOG / MU / GOOG 等自选股 |

这些标的**全部在新方案的常驻层**，杀掉等于明天重排。Plugin 没有取消队列的接口，要杀只能直接删 `ops_jobs.job_ingest`，那是 Plugin 的 schema，Research 按 D13 不写。结论：保留。

新核的枚举不必排在它后面：`worker/claim.py:122` 按 `priority DESC, created_at ASC` 取任务，核的 `option_contract` / `option_expiration` 以更高 priority 入队即可插队。

### 4.4 分步计划（三处已确认，① ② 已落地）

**① Research：规则与清单 —— 已落地（0.95.0，2026-09-08）**

`research.option_universe`，一行一个标的：

```sql
CREATE TABLE research.option_universe (
    symbol          text PRIMARY KEY,
    tier            text NOT NULL CHECK (tier IN ('resident','core','edge')),
    entered_on      date NOT NULL,
    last_seen       date NOT NULL,          -- 边：最后一次通过筛选；核：最后一次过阈值
    history_months  smallint NOT NULL,      -- 24 / 12
    reason          text NOT NULL,          -- 'watchlist' | 'benchmark' | 'liquidity>=200M' | 'sepa>=70'
    updated_at      timestamptz NOT NULL DEFAULT now()
);
```

主键用 `symbol` 而非代理键：一标的一行，所有 join 都按 symbol。这是与 Trade 侧 `<table>_id` 惯例的一处有意偏离，理由如上。

每日 Dagster asset `research/option_universe_refresh`：常驻 = 自选 ∪ 持仓 ∪ 基准；核 = 规则，进入 ≥ $200M、退出 < $120M（滞回 60%），每月首个交易日重估；边 = 当日幸存者，`last_seen` 超过 90 个交易日则移除。`db/calendar.load_symbols_from_env_or_query` 改为优先读此表。

**② Plugin：读 Research 清单 —— 已落地（0.16.0，2026-09-08，四个部署已滚完）**

- `scheduler/daily.py` 的 `resolve_watchlist_with_source` 来源链最前面加 `research`：`SELECT symbol, tier, history_months FROM research.option_universe`；读不到时回落到现有链（platform → cache → DB → fallback）。
- 期权 slot（约 944 行）按 tier 设 priority：常驻 > 核 > 边 > 回填；`option_backfill_plan` 的月数取 `history_months`。
- 首次加载 547 个核标的时按 priority 入队，不等当前回填。

**③ 引擎耗时（待量，核枚举完成后）**

575 个标的下游引擎（vol surface fit、GEX、VRP、terrain）的运行时长未测。核的枚举完成后，先跑一次 trading-day job 量时间窗，再决定要不要把 worker 加到 8 个。


## 5. 校准记录

| 快照 | 日期 | 说明 |
|---|---|---|
| 2026-09-08.1 | 2026-09-08 | 首次校准，对蓝图 v1.1 的 27 条契约。 |
| 2026-09-08.2 | 2026-09-08 | 基础层深校准：覆盖矩阵、三个死 lens 的根因、两个宇宙、缺批量原语；§3b 列出拉近差距的三类选项供讨论。 |
| 2026-09-08.3 | 2026-09-08 | 丙的折中方案：成本单位实测、三层宇宙规则、两档总量与代价、归属、未量风险。 |
| 2026-09-08.4 | 2026-09-08 | Owner 拍板 $200M 与三层方案；核修正为 547（限普通股）；队列实测 15 小时可跑完、全在常驻层，结论保留；分步计划与 DDL 草案待确认。 |
| 2026-09-08.5 | 2026-09-08 | ① ② 落地：Research 0.95.0（表、引擎、asset、resolver、乙）与 Plugin 0.16.0（`universe: research`、tier 优先级、按行回填月数、每次 150 个新名字）；首次枚举已排入 173 个任务。 |
