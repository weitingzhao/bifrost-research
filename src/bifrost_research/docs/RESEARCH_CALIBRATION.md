---
version: 2026-09-08.1
updated: 2026-09-08
status: 快照 · 排序待讨论
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

## 2. 契约状态

### 基础层

| 编号 | 状态 | 证据 |
|---|---|---|
| C-F1 | ⚠️ | `lenses/exhibits` 是统一入口；`copilot/harness/universe/{sepa,momentum,events}.py` 用自己的 SQL 重做了筛选 |
| C-F2 | ✅ | `engines/`、`lenses/` 无 objective 依赖 |
| C-F3 | ✅ | `lenses/registry.py` 9 个 spec |
| C-F4 | ❌ | 见 §1.2；task 已开 |
| C-F5 | ❌ | 见 §1.1；宇宙无规则 |

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

**计数**：✅ 16　⚠️ 4　❌ 6　⏳ 1，共 27 条。

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

## 4. 排序讨论（待开）

先深还是先宽、先接缝还是先实绩，在这里记结论。蓝图不讨论顺序。

## 5. 校准记录

| 快照 | 日期 | 说明 |
|---|---|---|
| 2026-09-08.1 | 2026-09-08 | 首次校准，对蓝图 v1.1 的 27 条契约。 |
