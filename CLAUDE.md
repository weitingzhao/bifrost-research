# CLAUDE.md — bifrost-research

与本项目用户的所有对话一律使用中文。

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
| 编排 | `src/bifrost_research/orchestration/` — **Dagster Wave 5.1**（dbt + engines + AI） |
| K8s | namespace `research`（`k8s/api/`；`k8s/engines/` CronJobs；`k8s/orchestration/` Dagster stub replicas:0） |
| D10 | **BLOCKED** — 不写交易执行路径 |

### Wave 2–4 所有权（Wave 6.4+ 统一 `features.*`）

- **`features.option_metric_*`**：volatility（IV / PCR / Max Pain / GEX）
- **`features.option_surface_*` / `features.option_flow_*`**：IV surface / order flow
- **`features.stock_signal_*`**：momentum / SEPA projection / event_radar
- **`features.stock_forecast_*`**：terrain / forecast session / hourly
- **`features.stock_backtest_*`**：settlement / results
- CronJobs：`k8s/engines/cronjob-volatility.yaml` + `k8s/engines/cronjob-engines.yaml` + `k8s/engines/cronjob-intraday.yaml` + **`cronjob-event-radar.yaml`**（W3–W4 + news ingest；镜像 tag `0.5.7`）
- LLM：`engines/forecast/llm.py` 可插拔（OpenAI/Anthropic/Ollama）；默认 **heuristic** 离线可测

### Wave 5.1 — Dagster 编排

依赖链（文档化）：

```
Plugin market ingest (external) → dbt (dw_stock.*) → Python analytics → AI forecast
```

| 项 | 说明 |
|----|------|
| Definitions | `bifrost_research.orchestration.definitions:defs` |
| dbt | `dagster-dbt` 读 `src/bifrost_research/dbt/target/manifest.json`（缺失则跳过 dbt assets） |
| Engines | volatility · momentum · gex · surface · flow · terrain · forecast · event_radar · backtest |
| Extra | `pip install -e ".[orchestration]"`（dagster + dagster-dbt + dagster-webserver） |
| 本地 UI | `make dagster-dev` → http://127.0.0.1:3000 |
| K8s | `k8s/orchestration/dagster.yaml` — webserver/daemon **replicas: 0** stub |
| 版本 | **`0.7.0`** — Feature Store `features.*` unify + SEPA projection (Waves 6.4–6.6) |

**Runtime Ignition 2026-08-21 DONE**：`research` NS + `research-api:8795` + dbt/volatility/engines/intraday CronJobs 已在 k3s 出数；platform-api `/api/v1/research/status` reachable。Dagster 仍 `replicas: 0`。

**Event Radar news ingest (decision A) DONE**：Research-workspace `事件雷达工作流/input/` → Cron `research-engines-event-radar` → `features.event_signal_radar_daily`。文档：`docs/EVENT_RADAR_INGEST.md`。

**Wave 5 foundation（已到位）**：Dagster 为 **optional** extra（`[orchestration]`）+ K8s stub（replicas:0）；Ops Console 侧 Research / 管线治理 catalog 已接通。生产启用仍见下方 blockers。

**生产 Dagster 部署 blockers**：

1. 需 Postgres / SQLite **Dagster instance storage**（run history / schedules）— 尚未配置
2. 镜像需含 `[orchestration]` 依赖；当前 Dockerfile 可能未装 dagster
3. `dbt parse` 产物 `manifest.json` 需进镜像或 init container
4. Secret / PG 凭证、daemon + webserver 联调；replicas 仍为 0
5. CronJob → Dagster schedules 迁移与 Owner 验收

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
- 版本：`0.30.0`（program research-copilot-reach — `GET /research/copilot/sessions` 新增 `q` 全文检索（title + messages 内容）；
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
