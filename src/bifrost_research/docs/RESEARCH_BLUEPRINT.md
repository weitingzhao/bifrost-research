---
version: 1.0
updated: 2026-09-08
status: draft — §3 待 Owner 拍板
---

# Research 蓝图

> **这是目标，不是功能清单。** 它回答"Research 应该是什么"，然后拿代码来对。
> 章节编号与契约编号（`C-F1` 这种）是**稳定锚点**，内容可以改，编号不改，日后校准按编号引用。
> 同一份文件：仓库 `bifrost-research/src/bifrost_research/docs/RESEARCH_BLUEPRINT.md`，
> API `GET /research/docs/blueprint`，UI `/docs/research-blueprint`。三处一个源。

## 0. 怎么用这份文档

**校准流程**，每次都一样：

1. 逐层看 §4 的契约表，对每条契约回答"代码今天满足吗"，写下证据位置（文件、行、或一条查询）。
2. 不满足的，写出**满足契约的最小改动**。不是重写，很多东西已经在了，只是没被这样命名和约束。
3. 差距按 §3 的决定排序：深与宽选哪一支，决定哪些差距先修、哪些根本不修。
4. 修完回到第 1 步。契约表里的"状态"列是校准的输出，每次校准更新一次。

**改这份文档的规则**：可以改内容、加契约、改状态；不改已有编号；每次改动在 §7 记一行。

## 1. 一句话定位

Research 是 Bifrost 的**决策载荷**：它产出判断，不产出订单。交易执行冻结（spine D10）；它只写 `dw_stock.*`、`features.*`、`research.*`，只读 `raw_market.*`，不碰 Trade DB（D13）。

它对人的价值，按重要性排序：

1. 说出人不知道的东西。
2. 说出的东西事后被证明是对的。
3. 说清楚为什么。

候选数量、自动批准资格、跑了多少次，都不在这个列表里。

## 2. 四层模型

三个面向人的姿态，加一个它们共用的记忆。依赖方向从下往上，**和 UI 里 Level 1→3 的编号方向相反**：编号最高的那层用系统最少。这是 2026-09-08 校准发现的核心错位，也是这份蓝图存在的原因。

```
                ┌────────────────────┐
                │  操作面 · Copilot   │  ← 所有人要读、要聊的东西的终点
                └─────────┬──────────┘
                          │  报告回流
            ┌─────────────┴──────────┐
            │  智囊 · Analyst        │  ← objective 驱动的编排，产出=报告
            │  （今名 Autopilot）     │
            └─────────────┬──────────┘
                          │  只经 lenses 读
  ┌───────────────────────┴─────────────────────────┐
  │  基础层 · Foundation                             │
  │  engines + lenses + feature store               │
  │  （Workbench 页面是它最薄的一层皮）              │
  └───────────────────────┬─────────────────────────┘
                          │
  ┌───────────────────────┴─────────────────────────┐
  │  实绩层 · Record                                 │
  │  提过什么 · 批过什么 · 后来到底怎么样了           │
  └─────────────────────────────────────────────────┘
```

形状是**扇形加一条回边**：基础层分叉出智囊和 Copilot，智囊的报告回流到 Copilot。实绩层被三者读，被智囊和 Copilot 写。这个形状回答"新东西该放哪"：人要读要聊的落 Copilot；可复用的分析落基础层；智囊只留编排；凡是"后来怎么样了"落实绩层。

### 2.1 基础层 · Foundation

**是什么**：全部分析能力的**唯一实现**。`engines/`（volatility、momentum、gex、flow、forecast、backtest、signal_hit、candidate_outcome……）、`lenses/`（registry、exhibits、track_record）、`features.*` 特征表、`dw_stock.*` dbt 宽表。

**入口**：`lenses/exhibits.build_exhibit` 是读基础层的正门。0.72.0 已经让页面判定条、Copilot 的 `research.exhibit.get` 和 daily brief 走这一个入口，蓝图把它升格为规则。

**拥有**：每个 lens 的定义、band、读数、说明文案；每个引擎的计算；特征表的写入。

**禁止**：不知道 objective 是什么；不知道谁在问；不写 `research.*` 里的提案与审批。

**Workbench 的位置**：Workbench 的 16 个页面是基础层最薄的一层皮，直接把面摆给人看。它是一个界面，和 Copilot、智囊平级，**不是**基础本身。说"接上 Workbench 的能力"时，指的是 import `lenses/`，不是调 Workbench 的 API。

**完成标准**：任何一个分析面，全系统只有一份实现，三个姿态读的是同一份。

### 2.2 实绩层 · Record

**是什么**：系统对自己战绩的记忆。它不是分析能力，也不是交互，是让另外三层**可改进**而不只是可运行的那一层。

**拥有**：
- `research.candidate_pool`：提过什么，每条的当时样子（`lens_snapshot`）
- `research.candidate_outcome`：提出之后到底怎么样了（1/5/20 日，对 SPY）
- `features.stock_signal_lens_hit_daily`：每个 lens 触发后的 5/20 日命中
- `research.backtest_run`：回测跑过什么、结果如何
- 拒绝记忆（`decline_memory`）：Owner 说过"不"的名字和当时的读数
- `research.ai_action_log`：谁、何时、花了多少、做了什么

**为什么单独成层**：2026-09-08 数出来，"命中率 / 实绩"在代码里至少有 **10 个实现**，回答 4 个不同的问题（lens 命中率、候选来源命中率、回测胜率、预测命中率），名字还撞车：`lenses/track_record.fetch_track_record` 与 `harness/evidence._fetch_track_record` 名字几乎一样，答的是完全不同的问题。一个没有名字的层会被建十遍。而且这几轮修的所有真实缺陷都在这一层：20 日命中率整月全空、周六提出的候选永远判不了、Owner 的拒绝从没写进候选池。

**完成标准**：每一个"后来怎么样了"的问题有且只有一个定义、一个实现、一个表；提出的每个候选都会被结清；每个触发都会被回填命中。

### 2.3 智囊 · Analyst（今名 Autopilot）

**是什么**：按预设 objective 自动编排一次分析，产出一份**带证据、带判官意见、带失效条件的报告**，等人批。它不执行，不下单，不扩容（D10）。

**只做三件事**：
1. 按 objective 的 policy 决定这次看什么（宇宙、层、阈值）。
2. 调基础层拿读数，调实绩层拿战绩，调判官（两个模型）拿意见。
3. 写报告，进 Inbox；把提案写进实绩层。

**禁止**：自己实现任何分析；自己算任何命中率；用候选数量或自动批准资格衡量自己。

**产出的度量**（对应 §1 的三条）：
- 新信息率：报告里有多少名字是 Owner 最近 N 天没见过、也没拒绝过的。
- 事后正确率：报告提出的名字，结清后跑赢基准的比例。
- 可解释：每个名字都能回答"为什么选它、什么情况下错了"。

**命名**：Autopilot 这个名字和它做的事相反——它扣着所有候选等人批（2026-09-08 实测：8 个候选全部被绳子扣下，`auto_approve_eligible=false`）。它是智囊，不是自动驾驶。改名是 §5 的一项，比接基础层便宜，但要动 seat、路由、存储键。

**完成标准**：读基础层只经 `lenses/`；不含任何 SQL 直接读 `features.*`；报告质量有度量且在 Overview 上可见。

### 2.4 操作面 · Copilot

**是什么**：人机交互面。人问，它读基础层与实绩层回答；智囊的报告回流到这里被讨论；写操作经审批 token。

**它是终点**：所有人要读、要聊的东西最终落这里。Workbench 是不知道问题时去逛的地方，Copilot 是知道问题后去问的地方，智囊页面是配置和审计的地方，不是读报告的地方。

**现状**：63 个工具（53 个 research、10 个 trade 只读），直接 import `engines.backtest.regime_stats`、`engines.brief.synth`、`lenses/`。三层里**只有它真正站在基础层上**。

**完成标准**：智囊产出的每一样东西，Copilot 都能引用并解释；不重复实现基础层的任何分析。

### 2.5 三个姿态在 UI 里的样子

- 每个 seat 只带自己的页面，seat 跟着路由走（2026-09-08 落地）。
- 首页就是标题，没有不能点的分区文字（Portfolio 标准，2026-09-07）。
- Overview 是无座页，讲三个姿态本身；本蓝图从它进入。

## 3. 深与宽（待 Owner 拍板）

### 3.1 现状数字（2026-09-08）

| 分析面 | 覆盖标的 | 数据截止 |
|---|---|---|
| SEPA（股票） | 3,475 | 2026-09-05 |
| terrain / 期权扫描 | 28 | 09-07 / 09-04 |
| momentum / VRP / GEX | 27 | 09-04 |
| IV percentile | 26 | 09-04 |
| `raw_market.option_daily` 最近十天 | **19** | 09-04 |

九个 lens 里三个几乎不触发：skew 建表至今 1 行，terrain_regime 4 行，order_sentiment 0 行。

**这道缝今天正在让智囊塌掉**：composite 漏斗 3,475 → 43（SEPA）→ momentum 返回 0 → events 跳过 → 期权 overlay 跳过（快照过期 4 天）→ 24 → 8。原因不是 bug：momentum 宇宙 25 个标的，和 43 个 SEPA 幸存者交集 5 个，那 5 个里没有 grade A。多层筛选实际上是单层。

### 3.2 两支的代价

**深**：三十个标的做透。
- Research 侧改动：智囊接上 `lenses/`；实绩统一；报告度量。
- 数据不动。期权面对 27 个标的已经成立。
- 代价：SEPA 的 3,475 大部分成为无期权面的"半个标的"；smart 的部分只对 27 个名字成立。

**宽**：三千五百个标的都有期权面。
- 缺口在 **Plugin 的采集范围与 Massive 订阅**，不在 Research。Research 按 D13 只读 `raw_market.*`，自己加不了。
- 归 program `market-data-subscription-focus` 管。
- 代价：订阅、采集时长、存储；Research 侧先不用动，但智囊在数据到位前仍是 SEPA 单层。

**先深后宽**：先把 27 个标的的四层做对，宽度到位时只是宇宙变大，架构不变。这是我的推荐，理由是深的那支全部改动都在架构层，无论最后宽不宽都不白做。

### 3.3 决定

- [ ] 深
- [ ] 宽
- [ ] 先深后宽

Owner 签字：__________　日期：__________

## 4. 契约与完成标准（校准表）

状态：✅ 满足　⚠️ 部分　❌ 不满足　⏳ 代码已到位等数据。证据一律写"在哪能看到"。

### 基础层

| 编号 | 契约 | 状态 | 证据（2026-09-08） |
|---|---|---|---|
| C-F1 | 任何分析面全系统只有一份实现 | ⚠️ | `lenses/exhibits` 是统一入口；但 `copilot/harness/universe/{sepa,momentum,events}.py` 用自己的 SQL 重做了 SEPA / momentum / events 的筛选 |
| C-F2 | 基础层不知道 objective、不知道谁在问 | ✅ | `engines/`、`lenses/` 无 `objective` 依赖 |
| C-F3 | 每个 lens 有 registry 条目、band、说明、页面路由 | ✅ | `lenses/registry.py` 9 个 spec |
| C-F4 | 每个 lens 实际会触发 | ❌ | skew 1 行、terrain_regime 4 行、order_sentiment 0 行；task 已开 |

### 实绩层

| 编号 | 契约 | 状态 | 证据 |
|---|---|---|---|
| C-R1 | "命中率"有且只有一个定义与实现 | ❌ | ≥10 处实现，4 个问题，见 §2.2 |
| C-R2 | 提出的每个候选都会被结清 | ⏳ | 0.91.0 对齐基准腿；`not_elapsed=132` 等 bar；3/3 已判 |
| C-R3 | Owner 的拒绝被记住，回来时说出变化 | ✅ | 0.90.0 `decline_memory`；漏斗有 `decline_memory` 步；回填 3 条 |
| C-R4 | lens 触发的 5/20 日命中会被回填 | ✅ | 0.91.0 `engines/signal_hit_fwd_fill`；一次回填 1,310 行 |
| C-R5 | 提案、审批、花费各有一条账 | ✅ | `ai_draft`、`ai_action_log`、`persona_eval_spend` |

### 智囊

| 编号 | 契约 | 状态 | 证据 |
|---|---|---|---|
| C-A1 | 读基础层只经 `lenses/`，不含直接读 `features.*` 的 SQL | ❌ | `harness/data_sources.py` 直接 SELECT scan_daily / lens_hit_daily；只复用了 backtest 引擎 |
| C-A2 | 多层漏斗真的多层 | ❌ | 今日 run：momentum 0、events 跳过、overlay 跳过，见 §3.1 |
| C-A3 | 产出是报告，报告有度量 | ⚠️ | `compose_report` 有 why / price / settled / wrong_if；但系统以候选数与 `auto_approve_eligible` 自评 |
| C-A4 | 报告回流 Copilot | ✅ | `research.loop.list_runs / get_run / explain_candidate` + daily digest |
| C-A5 | 不执行、不下单、不扩容 | ✅ | D10 守卫；propose-only；绳子四道门 |
| C-A6 | 名字说的就是它做的事 | ❌ | 名为 Autopilot，实为等人批的报告 |

### 操作面

| 编号 | 契约 | 状态 | 证据 |
|---|---|---|---|
| C-C1 | 所有人要读要聊的东西以它为终点 | ✅ | Inbox、digest、verdict strip 都可进 Copilot |
| C-C2 | 能用到基础层的全部面 | ✅ | 63 工具覆盖 VRP、vol surface、opex、GEX、flow、forecast、backtest、playbook |
| C-C3 | 不重复实现基础层 | ✅ | `mcp/tools/*` import engines / lenses |
| C-C4 | 写操作经审批，账可追溯 | ✅ | 审批 token、`fill_tool_defaults`、bearer 解析 owner |

### UI

| 编号 | 契约 | 状态 | 证据 |
|---|---|---|---|
| C-U1 | 每个 seat 只带自己的页；seat 跟着路由走 | ✅ | `researchNavCatalog.seatForRoute`，21 个测试 |
| C-U2 | 同一个队列只数一次：徽章 = 页面 | ✅ | 0.92.0 `pending_decision_calls`；两侧独立算得 34/19/24 |
| C-U3 | 首页即标题，无不可点的分区 | ✅ | Research、Portfolio、Strategy 三个域 |
| C-U4 | 一个页面只亮一行（父子高亮除外） | ✅ | Objectives 不再借控制台路由 |

## 5. 已知差距（2026-09-08 快照）

优先级取决于 §3。下面按"先深后宽"排：

1. **C-A1 / C-F1** 智囊接上 `lenses/`，删掉 `harness/universe/*` 里重做的筛选。这是架构差距，其余多数差距是它的症状。
2. **C-R1** 实绩层统一：一个 `track_record` 模块，四个问题四个函数，其余全部改为调用。
3. **C-A3** 报告度量：新信息率、事后正确率，上 Overview。
4. **C-A2** 漏斗：在数据到位前，momentum / events 层在 stock_composite 模式下应报"不可用"而非静默返回 0。
5. **C-F4** 三个不触发的 lens：阈值还是上游数据，先查（task 已开）。
6. **C-A6** 改名 Autopilot → 智囊（英文待定）：seat、路由、存储键、文案。
7. **宽**（若选）：转 program `market-data-subscription-focus`。

## 6. 不在范围内

- 交易执行的任何部分（D10 BLOCKED）。
- 写 Trade DB、写 `raw_market.*`（D13）。
- Plugin 的采集逻辑：Research 只能提需求，不能自己拉数据。
- Ops Console：平台不知道 SEPA 是什么，这是设计（双飞轮）。

## 7. 修订记录

| 版本 | 日期 | 改动 |
|---|---|---|
| 1.0 | 2026-09-08 | 首版。四层模型、深与宽两支的代价、24 条契约的首次校准。 |
