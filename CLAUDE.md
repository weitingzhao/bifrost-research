# CLAUDE.md — bifrost-research

与本项目用户的所有对话一律使用中文。

## 工作区定位（2026-09-06）

| 项 | 值 |
|---|---|
| 域 / 载荷 | Research (OLAP) · **第二 payload（决策载荷）**，与 Satellite 平级；库 `bifrost_golden_source`（单实例） |
| 运行位置 | K3s `research` NS：research-api `:8795` / research-mcp / Dagster / engines CronJob；镜像由集群内 Kaniko 构建 |
| 发布链 | `bifrost-deliver-research`（mirror-sync → clone → kaniko → rollout）；Argo `bifrost-research` **自动同步 GitHub main** —— 先镜像后 manifest，顺序反了就是 ImagePullBackOff |
| D13 | 只写 `dw_stock.*` / `features.*`；只读 `raw_market.*`；不写 Trade DB；不触交易执行 |
| 仓库可见性 | GitHub **PUBLIC**（12 个 repo 全部公开）—— `.env`、Secret YAML、dump、kubeconfig、账户内容永不入库 |
| 硬边界 | D10 交易执行冻结（BLOCKED）· D13 三域边界 · 平台/业务解耦（Flywheel A/B） |
| 事实基线 | `../AGENT_FACTS.md`（§8c 运行时与安全事实）· 规则 `../CLAUDE.md`（§8 Claude Code 运行配置） |

**蓝图与校准（两份，分工不同）**：
- 蓝图 `src/bifrost_research/docs/RESEARCH_BLUEPRINT.md`：Research **应该是什么**。四层模型、编号契约 `C-F1…C-U4`、理想的宽与深与接缝契约。只在理解变了时改。
- 校准 `src/bifrost_research/docs/RESEARCH_CALIBRATION.md`：Research **现在是什么**。每条契约的状态与证据、差距与最小改动、排序讨论。每次校准都改。
- API `GET /research/docs/{blueprint|calibration}`；UI `/docs/research-blueprint`、`/docs/research-calibration`。
- 规则：状态符号只出现在校准里，测试守着；契约按编号引用；智囊是**判断层**，必须做分析与评级（C-A7），纪律只是不重新发明原语（C-A1）。

会话请在工作区根 `/stocks` 启动（加载治理层 hooks / auto mode / 共享记忆）；运行时与安全事实以 `../AGENT_FACTS.md` §8c 为准。

## 职责

**`bifrost-research`** — Bifrost 系统的 **OLAP 分析域**（Research Engine）。

承载选股/选期权分析、预测、回测与 AI 情报；与 Trade（OLTP）和 Ops（控制面）并列，构成三域架构。

### 三域定位

| 域 | Repo | 职责 | 数据库 |
|----|------|------|--------|
| Trade (OLTP) | `bifrost-trade-*` | 交易执行、持仓、实时监控 | `bifrost_{dev,stg,prod}` 环境隔离 |
| **Research (OLAP)** | **本 repo** | 分析、预测、回测、选股选期权 | **`bifrost_golden_source` 单实例** |
| Ops (Control Plane) | `bifrost-platform` | 环境治理、健康探测、部署编排 | 控制面状态（非业务库） |

### 架构概要

| 项 | 说明 |
|----|------|
| 包名 | `bifrost_research` |
| 输入 | `raw_market.*`（Plugin 写入，本域只读） |
| 输出 | `dw_stock.*`（dbt 人读）· `features.*`（Feature Store，19 表四段命名，Python engines 写） |
| dbt | `src/bifrost_research/dbt/` — SEPA 宽表管线（原 bifrost-analytics） |
| Engines | `src/bifrost_research/engines/` — volatility · momentum · gex · flow（W3）· **forecast / event_radar / backtest（W4）** |
| API | `src/bifrost_research/api/` — Research API `:8795`（SEPA + options + `/research/*` W3–W4 + elementary） |
| 编排 | `src/bifrost_research/orchestration/` — **Dagster**（批式养库：Plugin enqueue + dbt + projection + engines；`ops_dagster` instance storage） |
| K8s | namespace `research`（`k8s/api/`；`k8s/engines/` 剩余 Cron；`k8s/orchestration/dagster.yaml` replicas:1） |
| D10 | **BLOCKED** — 不写交易执行路径 |
| 养库边界 | 批式 Golden Source（Market / Flex / Research 全槽）→ Dagster multi-schedule；IB Client / 实时总线 → Deployment。Ground truth = signal-health asof，不是 Cron Complete。**全部养库 CronJob `suspend: true`**。 |

### Wave 2–4 所有权（Wave 6.4+ 统一 `features.*`）

- **`features.option_metric_*`**：volatility（IV / PCR / Max Pain / GEX）
- **`features.option_surface_*` / `features.option_flow_*`**：IV surface / order flow
- **`features.stock_signal_*`**：momentum / SEPA projection / event_radar
- **`features.stock_forecast_*`**：terrain / forecast session / hourly
- **`features.stock_backtest_*`**：settlement / results
- CronJobs：`k8s/engines/cronjob-volatility.yaml` + `k8s/engines/cronjob-engines.yaml` + `k8s/engines/cronjob-intraday.yaml` + **`cronjob-event-radar.yaml`**（W3–W4 + news ingest；镜像 tag `0.5.7`）
- LLM：`engines/forecast/llm.py` 可插拔（OpenAI/Anthropic/Ollama）；默认 **heuristic** 离线可测

### Wave 5.1+ — Dagster 批调度（Data Husbandry）

依赖链：

```
Dagster market_eod / flex_* (HTTP enqueue) → Plugin workers (ops_jobs.*)
  → husbandry_gate → dbt (dw_stock.*) → sepa_projection → engines → scan
```

| 项 | 说明 |
|----|------|
| Definitions | `bifrost_research.orchestration.definitions:defs` |
| Schedule | `research_trading_day_schedule` Mon–Fri 22:30 America/New_York |
| Instance | Golden Source schema `ops_dagster`（`scripts/ops_dagster_schema.sql`） |
| Extra | `pip install -e ".[orchestration]"`（dagster + dagster-dbt + dagster-webserver + dagster-postgres） |
| 本地 UI | `make dagster-dev` → http://127.0.0.1:3000 |
| K8s | `k8s/orchestration/dagster.yaml` — webserver/daemon **replicas: 1** · image `0.50.0-dagster` |
| 版本 | **`0.50.1`** — orchestration/status multi-schedule observe (`schedules_*` / `recent_failures`); 0.50.0 = Full husbandry multi-schedule migrate |

**仍留 Cron**：**无**（养库已全部迁 Dagster）。IB Gateway / Client / realtime WS = Deployment，不进 asset graph。

**已 suspend（由 Dagster 接管）**：Massive 全部 SLOT Cron；Flex trades/transactions；Research dbt / volatility / engines / scan / VRP / OpEx / SVI / intraday / settlement / event-radar / alert / signal-hit / canonical-pnl / agents / ensure-partitions / vol-weekly-backfill。

**生产点火步骤**：

1. Apply `scripts/ops_dagster_schema.sql` on Golden Source
2. `make dbt-parse && make build-image-dagster`；注入 write tokens 到 `bifrost-research-secrets`
3. `kubectl apply -f k8s/orchestration/dagster.yaml`
4. 确认 overlapping Cron 仍为 `suspend: true`（禁止与 Dagster 双写同一 `features.*` 日表）

**Event Radar news ingest (decision A) DONE**：Research-workspace `事件雷达工作流/input/` → Dagster `research_event_radar_schedule`（Cron suspended）→ `features.event_signal_radar_daily`。文档：`docs/EVENT_RADAR_INGEST.md`。

### SEPA 数据流 (Wave 6.5+)

| Layer | Schema / tables | Role |
|-------|-----------------|------|
| **业务 owner（dbt）** | `dw_stock.mart_sepa_*` | SEPA 四阶段宽表 — **人读 / dashboard / screener** |
| **投影 mart** | `dw_stock.mart_sepa_feature_daily` | dbt 稳定列子集 → projection task 输入 |
| **Feature Store** | `features.stock_signal_sepa_daily` | **模型 / backtest 读** — `asof_ts` = **last projection timestamp** (daily UPSERT; NOT historical PIT snapshot) |
| Research API | `/research/sepa/*` → `mart_sepa_screener_wide` | 人读 |
| Research API | `/research/sepa/model/*` → `features.stock_signal_sepa_daily` | 模型读 |

Dagster dependency chain:

```
Plugin ingest → raw_market.* → dbt (dw_stock.*) → projection → features.* → AI forecast
```

### 目录结构

```
src/bifrost_research/
  dbt/              # dbt-core SEPA analytics
  engines/          # volatility / momentum / gex / flow / forecast / event_radar / backtest
  api/              # FastAPI Research API — :8795
  schema/           # features.* Feature Store DDL
  db/               # Golden Source 连接 / upsert / calendar
  scheduler/        # CronJob entrypoints (volatility + engines)
  orchestration/    # Dagster Definitions + dagster-dbt + engine assets (Wave 5.1)
```

### Golden Source 纪律

- 单一 DB 实例：`bifrost_golden_source`（CNPG LAN NodePort `192.168.10.73:30432`）
- **只读** `raw_market.*`（由 Market Data Plugin 写入）
- **可写** `dw_stock.*` / `features_*` / `ops_dbt.*`（canonical Golden Source pipeline schemas）
- **禁止**写入 Trade DB（`bifrost_dev` / `bifrost_stg` / `bifrost_prod`）
- **禁止**触发交易执行（daemon control、`ib:operator:cmd`、live place_order）

## 命令

```bash
make install-dev              # pip install -e ".[dev]"
make install-orchestration    # + dagster / dagster-dbt / webserver
make run-api                  # Research API :8795
make db-init-analytics        # apply features_daily DDL (legacy make name)
make db-init-research         # apply features_* + research DDL
make dbt-run                  # dbt run（需 ANALYTICS_PG_*）
make dbt-test                 # dbt test
make dbt-parse                # 生成 target/manifest.json（供 dagster-dbt）
make dbt-docs                 # dbt docs generate && serve
make dagster-dev              # Dagster UI :3000（需 [orchestration]）
make dagster-defs             # smoke：打印 asset 数量
make lint                     # ruff + sqlfluff（有则跑）
make test                     # pytest
make event-radar-ingest       # file ingest → features_signals.event_radar（见 docs/EVENT_RADAR_INGEST.md）
make build-image              # Docker 全包镜像（engines + API）
```

### Event Radar ingest（Owner decision A）

| 项 | 值 |
|----|-----|
| Drop zone | `Research-workspace/事件雷达工作流/input/` |
| Env | `EVENT_RADAR_INPUT_DIR` / `EVENT_RADAR_ARCHIVE_DIR` / `EVENT_RADAR_ARCHIVE` |
| Entrypoint | `python -m bifrost_research.scheduler.event_radar` |
| K8s | `k8s/engines/cronjob-event-radar.yaml`（PVC `/data/event-radar`） |
| Docs | `docs/EVENT_RADAR_INGEST.md` |
| Table | `features_signals.event_radar`（D10 advisory / D13 OLAP） |

本地 API：

```bash
make install-dev
make run-api
# 或: uvicorn bifrost_research.api.app:app --host 0.0.0.0 --port 8795
curl -s http://127.0.0.1:8795/health
```

本地 Dagster：

```bash
make install-orchestration
make dbt-parse                # optional but enables dbt assets
make dagster-dev
# 或: dagster dev -m bifrost_research.orchestration.definitions
```

## 数据库连接（dbt / analytics / API）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANALYTICS_PG_HOST` | `192.168.10.73` | CNPG LAN NodePort |
| `ANALYTICS_PG_PORT` | `30432` | |
| `ANALYTICS_PG_USER` | `analytics_writer` | |
| `ANALYTICS_PG_PASSWORD` | — | K8s Secret / 本地 `.env` |
| `ANALYTICS_PG_DATABASE` | `bifrost_golden_source` | |
| `ELEMENTARY_REPORT_PATH` | `/report/elementary_report.html` | Elementary HTML 报告路径 |

## 修改纪律

- 公开 API / schema 契约变更需同步 Trade 消费者 + Ops Console catalog
- 不引入 `bifrost-core` pip 依赖（自研 `db.conn`）；不引入 Trade 业务逻辑
- 编排依赖用 optional extra `[orchestration]`，避免默认安装膨胀
- 新增 dbt 模型需更新 `_*__models.yml` 文档与测试
- D10 BLOCKED — 不涉及交易执行路径
- 版本：`0.95.1`（sepa / momentum 衰减 lens 只追踪 hot 侧 —— 0.95.0 的 cold 侧每天 400 个触发，30 天回补在 2Gi 的 pod 里 OOM（exit 137），且无人消费；momentum 的触发改为只看 A 级：A 级分数 77–85，registry 的 80 分带会切掉一部分；它今天稀疏是因为引擎只算期权宇宙 27 个名字，宇宙放宽后自然增长；D10 仍 BLOCKED）
- 版本：`0.95.0`（**蓝图 C-F5 / C-F3 落地** —— 期权宇宙从「Plugin 采到了什么」变成一条规则：新表 `research.option_universe`（主键 symbol，三层 resident / core / edge），引擎 `engines/option_universe` 每日刷新：常驻 = 基准 ∪ Plugin 自选并集 ∪ 已采集标的；核 = `dim_universe` 普通股 20 日均美元成交额 ≥ $200M，退出 < $120M 滞回；边 = SEPA SETUP/PIVOT ≥ 70 的当日幸存者，12 个月历史，90 交易日保留。首次填充 575 个（27 / 527 / 21）。`db/calendar.load_symbols_from_env_or_query` 改为优先读它。Dagster asset `engines/option_universe`。**乙**：sepa 与 momentum 两个最宽的 lens 加入衰减追踪（`decay_lens` + `hit_rule=follow`），hot 分别要求 SETUP/PIVOT 路径与 A 级；此前覆盖 3,475 个标的的那一层没有任何自我度量；D10 仍 BLOCKED）
- 版本：`0.92.0`（两件事同版：编排侧 `feat(orchestration): 0.69.0 — husbandry gate reads the doctor's EOD verdict; oi-gap-heal retired (P3)`（另一会话提交）；以及 **Inbox 的队列只数一次** —— 侧边栏徽章读 `pending_memos`（活跃 objective 的候选批次数），这个数在 objective 行上是对的，挂在 Decision Inbox 链接上就是错的：徽章说 3，点进去的页面有 34 个待决。新增 `standing.pending_decision_calls`，套用该页自己的三条规则（briefing 不算待决、同一批的重复算一次、批准后什么都不写的 policy_suggestion 不算待决）；第三条放在后端是因为**写入的是这一侧** —— 它同时拥有两份白名单并按作者选择，和 `api/agents.py` 批准时的逻辑一致，这在 Owner 路径能携带 `decline_memory` 这类模型不可提议的键之后才成立。实测两侧各自独立计算并一致：34 待决 / 19 折叠 / 24 briefing；D10 仍 BLOCKED）
- 版本：`0.91.0`（**让 Autopilot 说出新东西** —— Autopilot 三天只提过 12 个不同标的，09-06 与 09-07 是完全相同的 11 个，因为 `dismiss_draft` 只写 `ai_draft` 从不碰 `candidate_pool`（7 次 dismiss 后 28 个候选仍全是 `open`）；新增 `_dismiss_candidate_batch` 与 `copilot/harness/decline_memory.py`：被拒绝的名字只有在**变好**时才回来（分数上行 ≥ `min_score_delta`、path/grade 进阶、新事件、regime 翻转），卡片写明变了什么，无从比较则沉默；`decline_memory` 只进 `OWNER_POLICY_WHITELIST`，不进 `POLICY_SUGGESTION_WHITELIST` —— 提候选的模型不得提议放松压制它自己的门；`force_delete_run` 保留 dismissed 行。**账本从只写不判变成可判**：`candidate_outcome` 两条腿日期口径不一（候选腿取 ≥ trade_date 的首根 bar，基准腿要求 SPY 在 trade_date 当天有行情），周六提出的候选 `hit` 恒为 NULL 且永不重访；新增 `engines/signal_hit_fwd_fill`（对照 `vrp_fwd_ret_20d`，30 个交易日回补 `hit_5d/20d` —— 调度只走 3 天，而 `_fwd_return` 需要 horizon+1 根 bar，所以 `hit_20d` 从来没被写过非 NULL 值，`policy.min_hit_rate` 一直在读空表）。**归档期权线** `obj-morning-iv-hot-watch`（宇宙 25 个标的、无一 IV rank ≥ 70，描述里写明恢复条件）。**限定范围的副本审计**：`_ALLOWED_OBJ_STATUSES` 无任何读者且写错了词表（真实词表是 active/archived），改为 `OBJECTIVE_STATUSES` 并在 `set_objective_status` 强制 —— 认不出的状态在两个列表里都查不到；policy-suggestion 的 400 报的是模型的窄名单而校验用的是 Owner 名单；前端 `ObjectiveStatus` 与 `DraftKind` 同步纠正；D10 仍 BLOCKED）
- 版本：`0.77.0`（A1–D4 程序复盘的修复批 —— D10 聊天守卫的白名单曾对整条消息生效（一句「what is place_order」就让同一条里的 `daemon.scale` 不再被检查），改为只抹掉被引用的那一段；lean band 用了 extreme 的原话（IV Rank 62 和 95 逐字相同），改为同一断言加对冲前缀；`objective_outcome` 读 `approve_result`（无人写入的键，测试也钉着它），Cron 摘要因此永远说不出「已自动接受」；judge 阶段异常时 `{'status':'error'}` 被 `.get(...,True)` 读成可自动接受；`latest_digest` 优先取 pending，于是批准当日 digest 会让所有 hub 判定条退回前一天；**每个带非空默认值的写工具在聊天审批时必然 400 tampering**（token 按调用方发的参数签，工具按 FastMCP 补齐默认值后重算——`research.loop.run_objective` 自 D1 起就从未能在 Copilot 里执行），新增 `fill_tool_defaults` 在签发处一次性解决全部工具；`/approve` `/execute` `/dismiss` 与 approve-all 此前无 owner 依赖且把调用方自报的 `approved_by` 写进审计账，改为从 bearer token 解析；`/health` 新增 `approval_secret: env|dev_fallback`（回退常量写在这个 PUBLIC 仓库里，等于任何人都能签出有效审批 token）；Harness Console 的手动 Approve-all 静默套用 0.45 默认绳子下限而非 objective 自己的 `min_source_hit_rate`；`research-gex-intraday` / `research-settlement` 的 manifest 缺 `suspend`，与 Dagster 同分钟双写只差一次重建；D10 仍 BLOCKED）
- 版本：`0.76.1`（聊天审批写入其实没写：mcp 1.29 的 `FastMCP.call_tool` 返回 `(content, structured)` 元组，`_dispatch_tool` 两半都读不到 `.text`，解析成 `[]` 回 `{ok:true,data:[]}`，DiffApprovalCard 的 Approve 与 `POST /research/copilot/execute` 一直「成功」却零写入、账记 executed；D4 验收时 propose_candidate 返回 `data: []` 才发现；`tool_result_envelope` 按每种形状取工具自己的信封；D10 仍 BLOCKED）
- 版本：`0.76.0`（research-loop-automation **D4** —— Copilot 的判定回到页面：`GET /research/verdicts/{symbol}`（`copilot/agents/symbol_verdicts.py`）给出当日 digest 对该标的的行（lens band、是否被 Loop 提出、绳子的判定、judge 是否分歧、hypothesis 是否被规则 resolve）与全部相关提案的审批状态（candidate open/promoted/dismissed、hypothesis active/validated/rejected、decision/order 草稿 pending/approved、聊天审批账 proposed/approved/executed/error），与 Inbox 同源；前端 `CopilotVerdictStrip` 挂在 `LabHub` 的 context bar 下，四个 hub 全有，60s 刷新；D10 仍 BLOCKED）
- 版本：`0.75.0`（research-loop-automation **D3** —— 无人值守、拴着绳：`copilot/harness/leash.py` 逐候选判定（两模型一致 ∧ validate 未 block 且 net 为 support/caution ∧ selection 证据已测 ∧ 来源在最长已判 horizon 的结清命中率（≥5 个 outcome）≥ `policy.min_source_hit_rate`，默认 0.45，进两份 policy_suggestion 白名单）；`approve_all_for_run` 对 candidate_batch 逐名过绳：通过的成 hypothesis，其余留在草稿并写明原因、草稿保持 pending 等 Owner；取消 run 级「一个分歧全批不批」；每次判定记 `loop_auto_accept` 账；run outputs.approve_all 带 accepted/held，digest 逐批引用；`copilot/agents/weekly_policy_review.py` 周日 22:00 UTC（`research_weekly_policy_review_schedule`）按各 objective 自己候选的结清 outcome（`build_summary` 新增 `objective_id` 过滤）+ 最近一次 judge 结果出 policy_suggestion（弱则提案带证据、好则只记账），永不自动批准，`POST /research/agents/weekly-policy/run`；Trust 门仍只认平台矩阵的 L0（Owner 在 Console 授）；D10 仍 BLOCKED）
- 版本：`0.74.0`（research-loop-automation **D2** —— 每日一条 digest：`copilot/agents/daily_digest.py` 把 holdings ∪ 昨日以来候选的 exhibit 读数、Loop 状态（objective / run / 待批草稿 / trust）、按 objective+symbol 集折叠的候选批次、judge 分歧（每模型一行 + report 的 wrong_if）、outcome 规则昨日以来的 resolution 折成一条 `daily_digest` 草稿；事实由代码采集，DeepSeek 经 `resolve_chat_endpoint` 只改写文案（≈$0.001，成本记在 payload.prose），拿不到模型就用启发式 markdown；一天一条，重复跑为 no-op 除非 force；morning_prep 并入（digest 接管 11:30 UTC 的 Dagster 槽 `research_daily_digest_schedule`，asset 保留供手动）；新 draft kind `daily_digest`（approve 只读不写）；`POST /research/agents/digest/run` 手动发；D10 仍 BLOCKED）
- 版本：`0.73.0`（research-loop-automation **D1** —— Copilot 读并解释一次 run：MCP 读工具 `research.loop.list_runs` / `get_run` / `explain_candidate`（`copilot/harness/run_digest.py`：plan 来源、漏斗、hit_rate 门、每个候选各模型 judge 立场与一致性、report 的 why/price/settled/wrong_if、草稿；explain 再带候选行、hypothesis、已结清验证，只引用 run 自己的记录不重算）；写工具 `research.loop.run_objective`（dry_run 预览 objective/policy/plan/trust；执行需 Owner 审批 token，拒绝 curator batch pass 以防嵌套 run；与 HTTP batch-run 共用 `batch_orchestrate.start_async_batch`）；loop_curator 指令加「解释一次 run」流程，triage 把 Loop 问题路由到它，verdict 加 `loop_specialist` 工具；本机实测「why was RKLB proposed」只调一次 explain_candidate 并逐块带出处；D10 仍 BLOCKED）
- 版本：`0.72.0`（research-loop-automation **C3** —— Daily Brief 建在 exhibit 上：exhibit 构造核心从 `api/` 搬到 `lenses/`（`lenses/exhibits.py` 的 `build_exhibit` 是页面 verdict strip、Copilot `research.exhibit.get` 与 brief 的唯一来源；`api/exhibit.py` 只剩路由并 re-export 旧名）；`engines/brief/cards.py` 每张卡 = 一个 exhibit（registry 的 band/label/means、readings、as_of、track_record、hub 视图 `to`），brief 新增 vrp / skew / term_slope / opex 卡与顶层 `lenses`；verdict 的叙述来自 terrain exhibit，风险按 事件 → put wall 距离 → skew hot → backwardation → IV 极端 逐级取，灯 = 无读数灰 / as_of 等于所选日绿 / 更早黄；forecast_path exhibit 改为 AVG(ABS(close_miss_pct))（有符号均值让 NVDA 的 |miss| 显示 0.7% 而 Sessions hub 是 2.0%）；D10 仍 BLOCKED）
- 版本：`0.71.0`（research-loop-automation **C1+C2** —— C1：registry `page_route` 指向 5 个 Analyze hub（`/research/vol-regime?view=…` 等），brief 链接同步；C2 让每个 exhibit 说出比 band 更深的一句：skew 按该标的自己 252 日 |ATM slope| 百分位判定（`slope_pctile_252d` / `history_days`，<60 日给 caveat，signal_hit 触发同口径）；term_slope 用 near−far 分 backwardation / contango（`backwardation`、`term_structure`）；opex_pin 带 `history_summary.{cycles,pinned,pin_rate,median_abs_distance}` 的 pin 历史；gex_regime 附 `readings.vrp_link`（RV20 vs IV30 与 gamma 说法是否一致）；vrp 附 `history_summary.fwd20_by_band`（≥80 / ≤20 后 20 日中位与正比例）；新增 `GET /research/signal-decay/by-symbol`（一个 lens 多标的各自命中率）与 `GET /research/forecast/calibration`（按 terrain regime 的命中率 vs 宣称概率）；D10 仍 BLOCKED）
- 版本：`0.70.1`（research-loop-automation **B4** —— harness CronJob 去掉 `BIFROST_LOOP_OBJECTIVE_ID`，`--schedule=daily_open` 下所有 active objective 都跑；一个 objective 失败只记它自己（回滚事务、逐 objective 一行摘要、有一个成功就退出 0，避免 backoff 重跑把成功的批次提两次）；validate_hook 按 hypothesis 的 instrument 选模板（option-lens objective / option tag → `short_strangle_30d`，期权历史不足时回落 stock leg 并在 rationale 里说明）；证据按标的已由 0.65.8 修复并在 0.69.2 的真实 run 上核实（HALO 0.8 / NVDA 0.2 / EE 0.4 …）；D10 仍 BLOCKED）
- 版本：`0.70.0`（research-loop-automation **B3** —— hypothesis 按 outcome 规则自动 resolve（D-RLA-2）：`policy_json.resolution {enabled, horizon_days=20, validate_excess=+3%, reject_excess=−3%, benchmark=SPY}`，有 candidate 血统的 hypothesis 在窗口结清后 excess ≥ +3% → validated、≤ −3% → rejected，证据写入新列 `hypothesis.resolution_json` 并记 `ai_action_log`（`hypothesis_auto_resolve`）+ 一条 `eod_verdict` 简报；死区内仍走草稿并附 excess；`resolution` 进 policy_suggestion 白名单；Dagster eod_review 依赖 `engines.candidate_outcome` 先结算；`origin_ref` 新增 `objective_id`；D10 仍 BLOCKED）
- 版本：`0.69.3`（Owner 决定：judge 上短绳 —— `BIFROST_PERSONA_EVAL_MAX_TURNS`（CronJob 设 4，默认 8）限制每次 verdict 的回合数，DeepSeek 日上限降到 $0.50；trace 记 `judge_max_turns`；D10 仍 BLOCKED）
- 版本：`0.69.2`（judge 超时有名字：asyncio 的 TimeoutError 没有 message，trace 里只剩 "judge failed"；`estimate_cost` 按名字给 gpt-4o-mini / 4.1-mini / nano 定价 —— 家族表把所有 gpt* 按 GPT-4o 计价，首次双 judge 跑 8 个标的记了 $0.67，实际不到 5 美分，再跑几次就会用幻影花费把日上限打满；CronJob：单标的 120s、judge 阶段 900s；D10 仍 BLOCKED）
- 版本：`0.69.1`（judge 阶段归 policy 管、plan 不能删 —— 0.69.0 在 DEV 的第一份 LLM plan 没写 `persona_evaluate`，整批候选没经 judge 就进了 Inbox；`want_persona = policy.persona_evaluate`，prompt 把该步写进推荐顺序；D10 仍 BLOCKED）
- 版本：`0.69.0`（research-loop-automation Wave B1+B2 —— **B1** plan 链 `deepseek-chat`(json, 60s) → `gpt-4o-mini` → heuristic，每一跳记进 `llm_attempts`，回退原因逐跳点名（此前 reasoner 不吃 json 模式 + 15s 超时，DEV 每次都退回模板）；**B2** 每个候选由 `PERSONA_EVAL_MODELS`（默认 deepseek-chat + gpt-4o-mini）并行评估，`net_stance` 只取一致意见否则 `dissent`，validate 取最严厉者，失败的 judge 记 `heuristic_fallback` 且算 dissent；每个 provider 独立日上限 `PERSONA_EVAL_DAILY_CAP_USD_{DEEPSEEK,OPENAI}`，花费写 `ai_action_log`（新增列 `provider` / `cost_usd`，`action_kind=persona_eval_spend`）并在每次 run 起点回放；`persona_eval.py` 拆成 `persona_heuristic` / `persona_judge` / 入口；harness CronJob 开 `BIFROST_PERSONA_EVAL_AGENTS=1`；D10 仍 BLOCKED）
- 版本：`0.66.1`（policy_suggestion 带 `current_policy` 快照 —— 缺它时 Inbox 差异表每行的 Current 都显示「未设置」，8 → 10 读起来像从无到有；快照记录的是提议者当时看到的值）
- 版本：`0.66.0`（可调节的交易系统 —— `POST /objectives/{id}/policy-suggestion` 让 Owner 也能发起 policy 变更；走 draft 而非直写,系统提的和 Owner 提的留同一份记录,漂移才可归因;白名单在入口校验而非批准时静默丢弃;用严格 `LoopPolicy.model_validate` 而非 fail-soft 的 `parse_policy`;D10 仍 BLOCKED）
- 版本：`0.65.8`（`validate_hook` 取出 symbol、校验它存在、然后用空 params 跑回测 —— 每个 hypothesis 都按同一个全市场数字判定。实测:全市场 win_rate 0.3243(37 事件)判 rejected，而 WT 自己是 0.6(5 事件)、SCCO 0.5(4 事件)→ **判定被反转**，且慢 8 倍；另 `event_count` 从来不是 summary 的键(应为 `n_events`)，所以每条 verdict 都印 events=n/a 藏住了样本量；存量:15 条无 symbols 的 backtest_run + 16 条基于它的待批 verdict；D10 仍 BLOCKED）
- 版本：`0.65.7`（阶段计时照出 `run_backtest` 每次 29s：事件定义无参数、查询看不到候选，所以每次算同一个全市场答案（`n_events=37 win_rate=0.3243`，正是 curator 说的 scaffold 数）→ 按天 memo，29.07s→0.00s；payload 加 `scope: market_wide` 说明它不是本批标的的记录；holdings 负缓存 TTL 60s→900s（run 间隔通常超 60s，等于没缓存）；D10 仍 BLOCKED）
- 版本：`0.65.6`（Trust 链第三处断点：CronJob 的 `PLATFORM_API_URL` 指向 `platform-api.platform.svc`，而 `platform` namespace 是空的 —— 真实服务在 `bifrost-platform-prod`，所以连读矩阵都一直失败；改用窄权限 `PLATFORM_REPORTER_TOKEN`，绝不回退 operator token（operator 含 cluster scale 与 trust override，触 D10）；D10 仍 BLOCKED）
- 版本：`0.65.5`（Trust 闭环 LO-4 补齐 —— batch 跑完向平台 `POST /agent/governance/skill-runs` 登记结果；`trust_gate` 此前读 `effective_level`/`level`，而矩阵发的是 `current_level`，读到空串所以门永远关着；需要 `PLATFORM_OPERATOR_TOKEN` 才上报，缺则跳过不影响 run；D10 仍 BLOCKED）
- 版本：`0.65.4`（trace 事件加 `at_ms` → Pipeline 每阶段耗时，已完成的 run 也能看出时间花在哪；首次用它就发现 persona_evaluate 占 5.02s/5.12s，根因是本机解析不了集群内名 `api-monitor.bifrost-prod.svc`，DNS 阻塞 ~5s 且 HTTP timeout 管不到 → 失败探测缓存 60s；D10 仍 BLOCKED）
- 版本：`0.65.3`（漏斗补最后一刀 `max_candidates`：resolver 取 `max_candidates*3` 供 discovery_assist 否决，run_objective 再截到 `max_candidates`，这一步此前不记账 —— 提出 8 只的 run 漏斗停在 24，控制台照 24 报，产出虚报三倍；端到端断言末段 == `propose_candidates.count`；D10 仍 BLOCKED）
- 历史：`0.65.2`（batch-run 异步立刻返回 run_id → Pipeline 直播；Force delete 级联；D10 仍 BLOCKED）
- 历史：`0.65.1`（Harness Force delete + UI batch-run + mid-run trace live progress；D10 仍 BLOCKED）
- 历史：`0.65.0`（Harness Policy × Personas E2E — persona_eval 链、discovery_assist、Trust 无 dissent 才 auto-approve、outcome→policy_suggestion 飞轮；默认 heuristic；holdings 只读 overlay + UI mode 标注；D10 仍 BLOCKED）
- 历史：`0.64.3`（canonical_pnl Dagster job 失败：`dw_stock.mart_canonical_pnl_daily` 已是 dbt view，dual-write ON CONFLICT 无 PK；引擎只写 `features.*`，DDL 补 features 表 PK）
- 历史：`0.64.1`（Research Engine 修复 — dbt 第一次真跑就暴露三处旧问题：`us_market_holiday` 测试写了不存在的列名 `date`（实为 `holiday_date`，测试 ERROR 拖垮 build）、`period_type` 白名单漏了 4138 行合法的 `ttm`；
 `engines/forecast` 被 `_EXCLUDED` 排除两次又不属于任何 schedule —— **完全没有生产者**，sessions 停在 08-28，settlement 从此每次 SUCCESS 写 0 行；补 `research_forecast_schedule`；
 `load_upstream_signals` 四处日期查询改为「不晚于该日」—— 这些表由夜间批次为**前一交易日**写入，intraday 却查「今天」，spot 恒为 0、25 个标的全跳过；
 objective / run 支持归档·恢复·删除，有引用时 409 拒绝）
- 历史：`0.63.0`（composite 漏斗从宇宙规模起算（3472→47）而非 SEPA 输出（47→47）—— 后者让每次运行都读作 watchlist 规模）
- 历史：`0.62.1`（候选行写入 score —— 此前 draft 卡显示 82.8 而候选池同一标的显示 —）
- 历史：`0.62.0`（候选池去重 —— 同日同源同标的再次提出是刷新而非新候选；已决定的不被复活。此前 104 行 / 16 标的 = 85% 重复）
- 历史：`0.61.1`（curator 在跑但 token 不是它的 batch pass —— 单独报错，指向转抄失败而非 token 格式）
- 历史：`0.61.0`（W5 — batch pass 拒绝原因分因报错：无 curator 上下文 / token 被改 / 截断 / 过期 / 错 run
 此前五种情况同报 `400: malformed approval token`，headless curator 无法诊断自己被挡的原因）
- 历史：`0.60.0`（W4 证据链 — 每个候选带「为什么选它 / 价格位置 / 期权视图 / 该来源历史命中率 / 失效条件」；
 `analyze_symbol` 进 op 白名单，且 **plan 第一次真正驱动执行**（此前 plan 写进 trace 后从不被读）；
 期权分析对多数标的显式报 NOT MEASURED —— 空面板会被读成「这只股票没什么特别」）
- 历史：`0.59.2`（universe reach 按实际 active universe_mode 计算 —— 上线 stock_composite 后仍报 scan 的 28 就是新的陈旧指标）
- 历史：`0.59.1`（`policy_suggestion` 补进 ai_draft `_ALLOWED_KINDS`（Y.3 遗漏，LLM plan 一开就炸）+ 从调用点推导的 draft kind 契约测试）
- 历史：`0.59.0`（W3 stock_composite 可用 — events 层 text/date 比较修复（该列存 '待定' 故为 text）；
 10 处 fail-soft 处理器补 `rollback_quietly`（捕获 SQL 错误却不回滚 = 污染整个事务）；
 optional 层不得清空宇宙（仅在「层返回空」时生效，交集为空时照样清零）；Loop 宇宙 28 → 3,472）
- 历史：`0.58.1`（dbt 管线修复 — dbt 项目随包安装（此前非 editable 安装完全没有它，容器里从未生效）；
 镜像构建内 `dbt deps && dbt parse` 并断言 `load_dbt_assets()` 非空；Dagster 镜像获得独立构建流水线）
- 历史：`0.58.0`（Loop 可信度 W2 — 新建 `research.candidate_outcome`：候选被提出后到底怎么样了；
 结算引擎复用 signal_hit 形状，命中定义为「跑赢 SPY 同窗口」而非「涨了」（候选无方向，绝对胜率主要在量市场）；
 未到期的横期跳过而非写 0；`GET /research/candidate-outcome/{summary,rows}`；Dagster asset 接入 trading-day job）
- 历史：`0.57.0`（Loop 可信度 W1 — scan_legacy 漏斗在截断前计数（原来永远报 `3 → 3`）；
 零打分输入的行不得进入排名（该类行 100% 带中性默认 50，会压过真实算出的低分）；
 新增 `GET /research/universe/reach` 数据触达口径，某层不可读报 NOT MEASURED 而非 0）
- 历史：`0.56.2`（Waves LO-0…LO-4 + LS Stock-first — stock_composite 宇宙、白盒 FunnelStep、Curator、validate_hook）
- 历史：`0.50.1`（orchestration/status multi-schedule observe for Ops Console）
- 历史：`0.50.0`（Full husbandry multi-schedule — Massive UTC slots + Research aux; Cron all suspended）
- 历史：`0.49.3`（schedule default RUNNING；husbandry closed-loop）
- 历史：`0.49.2`（orchestration 探测分级 schema/permission/empty；ops_dagster SELECT GRANT）
- 历史：`0.49.1`（Research health layers — `GET /research/orchestration/status`；signal-health overall 含 stale；Console 三层 Pipeline health + 侧栏 research_olap 灯）
- 历史：`0.49.0`（Data Husbandry — Dagster batch schedule + plugin enqueue assets + sepa_projection；ops_dagster instance；重叠日批 Cron suspend）
- 历史：`0.48.4`（Loop Smartness 下一刀 — Approve-all 复用 Inbox `apply_draft_approval`（policy merge）；`candidate_batch` approve 建轻量 hypothesis + `promote_candidate`；harness `top_scan_symbols` 应用 `resolve_preset`；flag→decay lens 映射，unmapped/无 decay 行 skip 不当 failing；本机 research-api 加载工作区源码）
- 历史：`0.48.3`（Wave Z Loop Cleanup — harness/{gate,suggestion}.py 从 runtime.py 拆出（runtime 再 re-export 保兼容）；FE `components/research/harness/{CandidateBatchBody,PolicySuggestionBody}` 从 `cockpit/DraftCard.tsx` 抽出（308→147 行）；D10/D13 边界审计：harness/* 无 `ib:operator:cmd` / `place_order` / Trade DB 写入；写路径限定 `research.*` + `features.*`；新增 `tests/copilot/test_policy_suggestion_contract.py` 锁死 `plan_llm.POLICY_SUGGESTION_KEYS` == `objective_repo.POLICY_SUGGESTION_WHITELIST`）
- 历史：`0.48.2`（Wave Y.3 Loop Smartness — filter-scoped hit-rate gate（B3）+ awaiting_approval 不阻断 + candidate_batch draft 带 `hit_rate_warn`（C3）；`policy_suggestion` 独立 Decision Inbox draft，approve 时 `objective_repo.patch_policy_json` jsonb `||` merge 到 `objective.policy_json`（A1）；whitelist = `{preset, flag_filter, min_composite_score, min_hit_rate, max_candidates}`；LLM policy_suggestion 二重过滤（pydantic + repositories）；D10 BLOCKED — 仍不触交易执行）
- 历史：`0.48.1`（Wave Y.2 Loop Smartness — harness LLM plan step：`plan_llm.generate_plan_llm()` DeepSeek OpenAI-compat + Pydantic `LLMPlanResponse` schema（op 白名单 scan_universe/signal_decay_check/propose_candidates/await_approval）+ 15s timeout + 全 fail-soft；`plan.generated_by = "llm" | "heuristic"`；`policy_suggestion` advisory（不改 objective.policy_json）；env `BIFROST_HARNESS_LLM_PLAN=1` 或 `policy.use_llm_plan` 触发；`policy.llm_model` 可覆盖默认 `deepseek-reasoner`；D10 BLOCKED — plan 只描述路径不下单）
- 历史：`0.48.0`（Wave Y.1 Loop Smartness — harness runtime 接 `features.stock_signal_scan_daily` + `features.stock_signal_lens_hit_daily`；heuristic seed_symbols 退化为 fallback；scan/fallback/failed 三分支单测；policy_json 新增 preset / flag_filter / min_composite_score / min_hit_rate（后两个到 Y.3 生效））
- 历史：`0.47.1`（Wave X Loop Usability — `POST /research/drafts` owner-manual + Harness Console New Objective 弹窗 + Trade Opportunities Copy Payload → Open in form）
- 历史：`0.47.0`（Research Loop Maturity Z/R/C/A/O — candidate_pool + research.loop.* + harness propose-only + order_intent advisory；D10 BLOCKED）
- 历史：`0.43.0`（Analyze J/K/L/M — YAML align + opex_pin 0.01 + PortfolioTag + Signal Decay intersect/regime + AlertBell）
- 历史：`0.41.0`（Analyze J — integrity + opex threshold + pending）
- 历史：`0.40.0`（Analyze G/H/I — DEV deploy fill + Scan desk presets/flags + Signal Decay lens hit-rate + adaptive_30d）
- 历史：`0.38.0`（Analyze D/E/F — scan daily + playbook hit-rate + gex/regime lenses + forecast path overlay）
- 历史：`0.36.0`（Analyze B.2/C — similar-regime term_slope/pin_distance · forecast hit-rate · playbook trigger event-log · terrain history）
- 历史：`0.35.0`（IDS Waves 1–6 — Historical IV Solver dual-source）
- 历史：`0.34.0`（Waves 13–15 — hypothesis refresh-trajectory · similar-regime · signal-health · exhibit API/MCP；FE WatchlistHypothesisDetail / SignalHealth / AnalyzeVerdictStrip）
- 历史：`0.33.0`（Wave 14 — similar-regime + signal-health）
- 历史：`0.32.0`（Wave 13 — watchlist hypothesis loop / refresh-trajectory）
- 历史：`0.31.0`（Wave 12 — canonical PnL foundation）
- 历史：`0.30.0`（program research-copilot-reach — `GET /research/copilot/sessions` 新增 `q` 全文检索（title + messages 内容）；
 新增 3 个 Trade 只读 MCP 工具 `trade.strategy.gate_safety` / `trade.trading.position_attribution` / `trade.trading.performance`）
- 历史：`0.29.0`（Copilot `POST /research/copilot/stream` 可选 `client_context` — 注入 ephemeral system message，不写入 session 用户 turn）
- 历史：`0.28.1`（package version at Phase 1 start）
- 历史：`0.23.2`（Wave RS-KB QA · Q6 — `GET /research/copilot/models` 只返回后端实际配置的 provider（DEV cluster 只暴露 DeepSeek Chat / Reasoner），每个模型带中文 `note` 说明；前端 composer + Settings 从这个端点拉列表）
- 历史：`0.23.1`（Wave RS-KB QA — session `group_name` 列 + PATCH group/clear_group；stream `finally` flush-on-cancel（Stop 按钮 partial turn 落库））
- 历史：`0.23.0`（Wave RS-KB1→RS-KB5 — 全量 chat memory (turn frames)、multi-user bearer、Playbook rules/cases/notes、Curator agent、pgvector embedding stub）
- 历史：`0.18.2`（Wave RS-UX5 — 修复 tool_result `call_id` 匹配（PENDING 悬挂 root cause）+ MCP text envelope unwrap；`copilot_session.pinned` 列 + `update_metadata` + PATCH endpoint（rename/pin））
- 历史：`0.18.1`（Wave RS-F5.1 — 加 `trade.strategy.opportunities`；修正 `trade.strategy.instances` 路径 `/strategies/*`；portfolio agent instructions 强化多工具复合调用）
- 历史：`0.18.0`（Wave RS-F5 — `trade.*` read-only MCP tools + `portfolio` specialist agent；holdings-aware Copilot）
- 历史：`0.17.2`（agent_runtime outer catch → 显示实际异常类名，不再无差别报 "MCP connect failed"）
- 历史：`0.16.0`（Wave RS-E4 Copilot write tools + approval tokens + DiffApprovalCard；Cockpit E1–E4 complete）
