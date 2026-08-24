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
| C1-1 | 2319-2320 | SPX 日内剧本 — 四场景概率条 (Rangy/Bull/Bear/Squeeze) | E (playbook.py ScenarioProbabilities) | A (POST /forecast/sessions/compute) | -- | 6.3.2 | 概率条 UI 显示四段 + 百分比 |
| C1-2 | 2319-2320 | 四场景卡片 — 关键位 / 策略建议 / 止损 / LIVE 状态 | E (playbook.py HourlyPathCall + OptionStructureRec) | A (GET /forecast/sessions/{id}) | -- | 6.3.2 | 四张场景卡各显示关键位 + 建议文本 |
| C1-3 | 2321 | 价格扇形图 — 预测带 + 实际 spot 叠加 + 路径转换日志 | E (terrain intraday — 6.2 新增) | A (GET /terrain/intraday — 6.2 新增) | -- | 6.2 + 6.3.2 | SVG 扇面 + spot 线 + 转换表 |
| C1-4 | 2322 | IV Smile — OTM 散点 + 拟合曲线 + R² + ATM IV | E (surface.py smile_params + surface_points) | A (GET /volatility/smile + /surface) | -- (Discovery 内有近似) | 6.3.1 | 双面板: 散点+拟合 / 拟合分析 |
| C1-5 | 2323 | SPX 分析模型 — terrain 四维评分 + regime + close 预期 | E (terrain.py MarketTerrain) | A (GET /forecast/terrain) | -- | 6.3.1 | 三列 Card: 地形 / 预期 / IV 曲面 |
| C1-6 | 2323 | 收盘预期 — 预期/远期价格 + 置信区间 + 盘前机制 | E (terrain expected_close + gamma_zone) | A (GET /forecast/terrain) | -- | 6.3.1 | 中间 Card 数值面板 |
| C1-7 | 2324-2325 | 动能雷达 — 多股得分 grid + grade + path + 三时间帧 | E (momentum/radar.py score/grade/path) | A (GET /momentum/radar) | -- (SEPA tier 内有 momentum filter) | 6.3.3 | 6×N 卡片 grid + grade 颜色 + 筛选 |
| C1-8 | 2325 | 动能因子解读 — 因子含义 / 何时好 / 何时差 | E (factors_json 包含 z_sdt/z_v/accept_vwap 等 11 因子) | A (同上) | -- | 6.3.3 | 页面底部折叠解读面板 |
| C1-9 | 2326 | IV 雷达 — 三区域分桶 (高/中/低速动率) + 股票计数 | E (iv_percentile_daily) | A (GET /analytics/options/iv-percentile) | F (IvRadarPage.tsx 已有分桶表格) | 6.3.4 | 增强: 三区域 Card 显示数量 |
| C1-10 | 2327 | IV Gauge — 单股 circular gauge (IV Rank 0-100) + ATM IV + IV Pctl | E (iv_percentile_daily iv_rank_1y + iv_current) | A (同上) | -- | 6.3.4 | 新增 Gauge Grid 视图 + IvGauge 组件 |
| C1-11 | 2328 | 大单 / 多腿追踪 — 策略分类 + 到期/行权价/Greeks | E (multi_leg_trades DDL scaffold) | A (GET /flow/multi-leg, scaffold) | -- | 6.4.3 | DenseDataTable + top 大单 grid |

## Case 2: GEX + Forecast + Settlement

| # | 截图 | 业务能力 | Engine | API | Frontend | 目标 Phase | 验收 |
|---|------|---------|--------|-----|----------|-----------|------|
| C2-1 | 3652 | GEX 日内走势 — GEX by volume + spot overlay + key levels | E (gex intraday — 6.2 新增) | A (GET /gex/intraday — 6.2 新增) | -- (Discovery 内有日级 GEX) | 6.2 + 6.4.1 | SVG 图: 柱状 GEX + spot 线 + 水平线 |
| C2-2 | 3653 | GEX 横向 bars — Pos/Neg by strike + spot trace + 多时间帧 | E (同上 levels_json) | A (同上) | -- | 6.4.1 | 水平柱状图 + spot 轨迹 + 时间帧切换 |
| C2-3 | 3654-3655 | Forecast Run 列表 — Daily/Hourly/Weekly + Input Snapshot + Evidence | E (playbook.py ForecastSession) | A (GET /forecast/sessions) | -- | 6.4.2 | 左侧 Run sidebar + 右侧 detail |
| C2-4 | 3655 | Evidence Summary — Total/Flow Score/Flow Bias/Proxy/Market Bias + 标的 | E (ForecastSession narrative + terrain_json) | A (GET /forecast/sessions/{id}) | -- | 6.4.2 | Input/Evidence Card |
| C2-5 | 3657-3658 | Forecast Window Detail — Path Call + Expected Levels + Option Structure | E (HourlyPathCall + OptionStructureRec) | A (GET /forecast/hourly) | -- | 6.4.2 | DenseDataTable 7 列 |
| C2-6 | 3678-3683 | Settlement — Schwab Actual + Forecast Settlement (Direction/Path/Close) | E (settlement.py settle_session) | A (POST /backtest/settle, GET /backtest/settlement) | -- | 6.6 | Settlement 列 + badges |
| C2-7 | 3714-3715 | Settlement badges — Direction hit / Path shape / Close zone / miss tags | E (forecast_settlement path_hit/close_miss) | A (同上) | -- | 6.6.4 | DenseTag success/danger badges |
| C2-8 | 3660 | Order Sentiment — 全日名义金额 + 情绪条 + 到期/行权价集中度 | E (order_sentiment_daily DDL scaffold) | A (GET /flow/sentiment, scaffold) | -- | 6.4.3 | KPI 条 + 渐变情绪条 + 柱状图 |
| C2-9 | 3661 | Multi-leg 大单表 — 成交/合约/策略/VWAP/BID/ASK/DTE | E (multi_leg_trades DDL scaffold) | A (GET /flow/multi-leg, scaffold) | -- | 6.4.3 | DenseDataTable + 分页 |

## Event Radar Workflow

| # | 步骤 | 业务能力 | Engine | API | Frontend | 目标 Phase | 验收 |
|---|------|---------|--------|-----|----------|-----------|------|
| ER-1 | 01_拆解 | 原始文本 → events_raw (parse) | E (pipeline.py step_parse) | A (POST /event-radar/run 触发全流程) | -- | 6.5 | API 接收文本返回 PipelineResult |
| ER-2 | 02_清洗 | 去重 + 噪声过滤 | E (pipeline.py step_clean) | A (同上) | -- | 6.5 | kept_count + dropped_count 正确 |
| ER-3 | 03_打标 | 方向/确定性/情绪/主线/重要性 | E (pipeline.py step_tag) | A (同上) | -- | 6.5 | 各维度分布合理 |
| ER-4 | 04_出表 | 结构化导出 (13 列) | E (pipeline.py step_export) | A (GET /event-radar/events) | -- | 6.5.2 | DenseDataTable 显示事件表 |
| ER-5 | 05_自检 | 14 项质检 | E (pipeline.py step_self_check) | A (self_check 包含在 run 返回) | -- | 6.5.2 | 自检结果面板 |
| ER-6 | dashboard | 事件表 + 主线甘特 + 多空撕裂 + 前瞻日程 | -- (dashboard.html 是独立静态页) | A (6.5 新增 /events/batches/themes/calendar) | -- | 6.5 | 四区域看板 |
| ER-7 | schema | 主线注册表 — 增删改 + 退场 | -- (静态 md 文件) | A (6.5 新增 /events/themes) | -- | 6.5.2 | 主线分布面板 |

## 跨域基础设施

| # | 领域 | 能力 | 状态 | 目标 Phase | 说明 |
|---|------|------|------|-----------|------|
| X-1 | DDL | terrain_intraday 表 | -- | 6.2.1 | PK: (symbol, trade_date, asof_ts) |
| X-2 | DDL | gex_intraday 表 | -- | 6.2.1 | PK: (symbol, trade_date, asof_ts) |
| X-3 | Engine | terrain intraday 计算 | -- | 6.2.2 | 复用 compute_market_terrain + asof_ts |
| X-4 | Engine | GEX intraday 计算 | -- | 6.2.2 | 复用 compute_gex + asof_ts + levels_json |
| X-5 | K8s | intraday CronJob | -- | 6.2.3 | 每小时 09:30-16:00 ET |
| X-6 | K8s | settlement CronJob | -- | 6.6.2 | 每日 17:00 ET |
| X-7 | API | Research Engine API 集成 (FE → platform-api → :8795) | -- | 6.3+ | src/api/researchEngine.ts |
| X-8 | FE SVG | ScenarioFanChart / GexStrikeChart / ProbabilityBar / IvGauge | -- | 6.3-6.4 | 自定义 SVG, 不用 Recharts |

---

## 总计

- 能力条目: 27 条 (Case 1: 11, Case 2: 9, Event Radar: 7)
- 基础设施: 8 条
- Engine 已实现: 22/27 (81%)
- API 已暴露: 20/27 (74%)
- Frontend 已有: 1/27 (4%) — 仅 IV Radar 分桶表格
- **本轮交付重点: Frontend 26 条 + 基础设施 8 条**
