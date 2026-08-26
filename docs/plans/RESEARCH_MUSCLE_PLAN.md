# Research Muscle-Building Plan — Wave RS (Post R9)

**Status**: Draft · awaiting Owner sign-off before Phase RS-A1 kickoff
**Team size assumption**: 2 people
**Data cost policy**: Zero new data sources this program — Polygon (via Market Data Plugin) + IB Flex + manual event drop only
**Total duration estimate**: 8–9 weeks (Waves A + B + C); Wave D deferred
**Cross-repo scope**: `bifrost-research` (engines / API / DDL) · `bifrost-trade-frontend` (UI) · `bifrost-trade-infra` (nginx routing, if needed)
**Trade execution freeze (D10)**: BLOCKED — no phase touches live order placement

---

## Program Rationale

Post-R9 Research domain has 17 pages but they behave as isolated dashboards, not a research workflow. This program does three things in order:

1. **Wave RS-A** — Introduce a first-class `Hypothesis` object + Research Home page + workflow-based sidebar so the 17 existing pages become nodes in a research pipeline instead of standalone exhibits.
2. **Wave RS-B** — Add three high-value labs (VRP · Vol Surface · OpEx Cycle) that pure-analytics-fill obvious gaps versus institutional option quant desks, using data already in Golden Source.
3. **Wave RS-C** — Upgrade Backtest incrementally: event-driven query → realistic fills → walk-forward → link to Hypothesis. Not a rewrite.

**Wave RS-D** (Jupyter / MLflow / Dagster productionization) is scoped but deferred; revisit after C completes.

---

## Multi-Agent Execution Guide

Every Phase is written to be picked up by an independent agent without additional context. Each Phase declares:

- `depends_on` — Phases that must complete first
- `can_run_in_parallel_with` — Phases safe to run concurrently
- `files.new` / `files.modify` — exact paths
- `data_model_changes` — DDL touched
- `api_contract_changes` — routes added / modified
- `verify_cmd` — commands the executing agent must run before reporting completion
- `acceptance` — checklist for Owner sign-off
- `sign_off.required` — whether Owner must approve before next Phase in the chain starts

Follow `.cursor/rules/phase-execution.mdc`. In batch mode, sign-off phases still pause for Owner unless explicit pre-authorization is given.

**Parallelization snapshot**:

- Wave RS-A: Phase A1 → A2 (serial) · then A3 · A4 · A5 can run in parallel (3 agents)
- Wave RS-B: three labs B-VRP / B-Surface / B-OpEx are fully independent (3 agents in parallel; each has 2 sub-phases: engine → API/FE)
- Wave RS-C: C1/C2 can start in parallel · C3 depends on C2 · C4 depends on A2 + C1/C2/C3

---

## Owner Decisions (needed before RS-A1)

| ID | Question | Options | Recommendation |
|----|----------|---------|----------------|
| D-RS-a | Where does `hypothesis` table live? | (1) `bifrost_golden_source.research.hypothesis` — colocated with existing OLAP writes<br>(2) Trade DB `bifrost_dev.*` — closer to strategy_opportunity | **(1) Golden Source `research.hypothesis`** — Research is OLAP domain, cross-env viewable, matches D13 |
| D-RS-b | Sidebar restructure — replace or preserve legacy grouping? | (1) Replace 6 subGroups with new 4-stage taxonomy<br>(2) Keep 6 old subGroups + add new stage-based groups behind a toggle | **(1) Replace** — clean break; do not maintain two taxonomies |
| D-RS-c | Home page route | (1) `/research` becomes Home (Daily Brief moves to `/research/daily-brief` — already there)<br>(2) `/research/home` new route | **(1)** — `/research` is currently unrouted; make it Home |
| D-RS-d | Backtest event-query API path | (1) `POST /research/backtest/event-query`<br>(2) `POST /research/backtest/scenarios` | **(1)** — clearer intent |
| D-RS-e | Vol Surface fit method | (1) SVI 5-parameter (Gatheral)<br>(2) SABR<br>(3) Both, user-toggle | **(1) SVI only for v1** — SABR later if needed |
| D-RS-f | Wave order | (1) Strict A → B → C<br>(2) Interleave (start A, then B in parallel, C at end) | **(1) Strict** — A ships workflow that B and C plug into |

---

# Wave RS-A · Research Flow Skeleton

**Goal**: Turn 17 island pages into a workflow. Introduce `Hypothesis` as first-class object. Rebuild sidebar by research stage. Add Research Home.

**Duration**: 1 week · 2 agents

## Phase RS-A1 · Hypothesis DDL

- **Repo**: `bifrost-research`
- **Depends on**: — (starting phase)
- **Can run in parallel with**: —
- **Sign-off**: required (locks contract shared by RS-A2, C4)

### Goal

Add `research.hypothesis` table to Golden Source; extend `schema/ddl.py::ensure_research_tables()` so `make db-init-research` provisions it.

### Files

- **new**: none
- **modify**:
  - `bifrost-research/src/bifrost_research/schema/ddl.py`
  - `bifrost-research/tests/schema/test_ddl.py` (add smoke)
  - `bifrost-research/docs/CAPABILITY_MATRIX.md` (row for Wave RS)

### Data model

```sql
CREATE SCHEMA IF NOT EXISTS research;

CREATE TABLE IF NOT EXISTS research.hypothesis (
    id              text PRIMARY KEY,                    -- ULID or slug (e.g. "nvda-earnings-vol-crush-2026q3")
    title           text NOT NULL,
    thesis          text NOT NULL,                       -- 1-3 sentence markdown
    symbols         text[] NOT NULL DEFAULT '{}',
    tags            text[] NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'active',      -- active | validated | rejected | archived
    origin_page     text,                                -- e.g. "sepa-daily-core" | "event-radar" | "manual"
    origin_ref      jsonb,                               -- free-form: {symbol, event_id, setup_id, ...}
    linked_opportunity_ids   text[] NOT NULL DEFAULT '{}',
    linked_backtest_ids      text[] NOT NULL DEFAULT '{}',
    conclusion      text,                                -- filled when status flips to validated/rejected
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    retired_at      timestamptz
);
CREATE INDEX IF NOT EXISTS hypothesis_status ON research.hypothesis(status) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS hypothesis_symbols ON research.hypothesis USING GIN(symbols);
CREATE INDEX IF NOT EXISTS hypothesis_updated ON research.hypothesis(updated_at DESC);
```

### API contract changes

None (DDL only).

### Verify

```bash
cd bifrost-research
make lint
make test
make db-init-research   # requires .env with ANALYTICS_PG_PASSWORD; if missing, mark ok=skipped in report
```

### Acceptance

- `\dt research.hypothesis` in Golden Source shows the table
- `pytest tests/schema/test_ddl.py` passes
- `docs/CAPABILITY_MATRIX.md` has new "Wave RS-A" row

---

## Phase RS-A2 · Hypothesis CRUD API

- **Repo**: `bifrost-research`
- **Depends on**: RS-A1
- **Can run in parallel with**: —
- **Sign-off**: required (locks FE contract for A3, A4, C4)

### Goal

Add `research/hypothesis/*` CRUD routes to Research API `:8795`. Trade FE reaches this via existing `researchEngineUrl()` gateway.

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/api/hypothesis.py`
  - `bifrost-research/src/bifrost_research/repositories/hypothesis.py` (SQL layer)
  - `bifrost-research/tests/api/test_hypothesis.py`
- **modify**:
  - `bifrost-research/src/bifrost_research/api/app.py` (include router)
  - `bifrost-research/pyproject.toml` bump to `0.9.0`
  - `bifrost-research/docs/RESEARCH_UX_DECISIONS.md` (D-RS-a locked)

### API contract

```
GET    /research/hypothesis                     -> list (query: status, symbol, tag, limit, offset)
POST   /research/hypothesis                     -> create ({title, thesis, symbols, tags?, origin_page?, origin_ref?})
GET    /research/hypothesis/{id}                -> single
PATCH  /research/hypothesis/{id}                -> partial update (status, conclusion, symbols, tags, linked_*)
POST   /research/hypothesis/{id}/retire         -> soft delete (sets retired_at)
GET    /research/hypothesis/summary/active      -> count by status + top-N recent (for Home page)
```

Response envelope: `{ ok: bool, data: ..., error?: string }` (matches wave4 style).

### Verify

```bash
cd bifrost-research
make lint
make test
uvicorn bifrost_research.api.app:app --host 0.0.0.0 --port 8795 &
curl -s http://127.0.0.1:8795/research/hypothesis?limit=5 | jq
```

### Acceptance

- `pytest tests/api/test_hypothesis.py` passes with in-memory / mocked DB
- Manual curl round-trip create → get → patch → retire works
- `bifrost-research` version 0.9.0 tagged (only after Owner accept)

---

## Phase RS-A3 · FE Hypothesis Hooks + Save-as-Hypothesis Button

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: RS-A2 (API contract stable)
- **Can run in parallel with**: RS-A4, RS-A5
- **Sign-off**: not required (FE-only; A4 consumes these hooks)

### Goal

FE data layer + a Save-as-Hypothesis UX affordance on 4 Discovery pages.

### Files

- **new**:
  - `src/api/researchHypothesis.ts` — thin fetch wrappers
  - `src/hooks/useHypotheses.ts` — TanStack Query list/get/create/patch/retire
  - `src/components/research/SaveAsHypothesisButton.tsx` — icon button + `Dialog` form
- **modify** (add Save button to page toolbar):
  - `src/pages/research/DailyBriefPage.tsx`
  - `src/pages/research/EventRadarPage.tsx`
  - `src/pages/research/SepaDailyCorePage.tsx` (SEPA Daily Core)
  - `src/pages/research/IvRadarPage.tsx`

### Behavior

- Button pre-fills `title`, `symbols`, `origin_page`, `origin_ref` from the page's current context
- On success, invalidate `['hypothesis', 'active']` query
- Uses shared `ConfirmDialog` pattern for the create modal (no `window.alert`)

### Verify

```bash
cd bifrost-trade-frontend
npm run lint && npm run build && npm run check:legacy-css
```

### Acceptance

- Save as Hypothesis button visible on 4 pages; opens Dialog; submits successfully against local Research API
- Newly saved hypothesis appears in `GET /research/hypothesis?limit=5`

---

## Phase RS-A4 · Research Home Page

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: RS-A2
- **Can run in parallel with**: RS-A3, RS-A5
- **Sign-off**: not required

### Goal

New route `/research` = Research Home. Aggregates Discovery hits from 4 sources + shows active Hypotheses.

### Files

- **new**:
  - `src/pages/research/ResearchHomePage.tsx`
  - `src/components/research/HypothesisCard.tsx`
  - `src/components/research/DiscoveryHitList.tsx`
  - `src/hooks/useResearchHomeData.ts` (aggregates: SEPA daily hits, Event Radar today, IV Radar extremes, Order Sentiment anomalies)
- **modify**:
  - `src/lib/router.tsx` — add `/research` route → `ResearchHomePage`
  - `src/layout/navConfig.ts` — add Home entry (RS-A5 will reorder groups)

### Page composition (per dense-ui-system rule)

- Verdict Strip: "N active hypotheses · M new discoveries today · K backtests running"
- Section 1: **Active Hypotheses** — 5 latest cards (`HypothesisCard`)
- Section 2: **Today's Discoveries** — 4 columns (SEPA hits · Events · IV extremes · Sentiment anomalies) each with top 3 items → each row has a "Save as Hypothesis" quick action
- Section 3: **Recent Backtests** — link-out to Backtest page (placeholder until RS-C4 fills it)

Uses `PageShell` + `PageHeader` + existing `data-display` primitives (`DenseDataTable`, `EmptyState`, `DenseTag`).

### Verify

```bash
cd bifrost-trade-frontend
npm run lint && npm run build && npm run check:legacy-css
# Manual: http://127.0.0.1:5173/research renders without console errors
```

### Acceptance

- `/research` shows all three sections; empty states graceful when no data
- Clicking a Discovery item Save-as opens Dialog (uses A3 button)
- Clicking a Hypothesis card jumps to `/research/hypothesis/{id}` (RS-A6 detail page — see below or defer)

---

## Phase RS-A5 · Sidebar Restructure (Stage-based)

- **Repo**: `bifrost-trade-frontend`
- **Depends on**: RS-A4 (Home route exists)
- **Can run in parallel with**: RS-A3
- **Sign-off**: required (visible UX change to Owner)

### Goal

Reorder `NAV_GROUPS[Research].subGroups` in `navConfig.ts` from object-oriented to stage-oriented.

### Files

- **modify**: `src/layout/navConfig.ts`

### New subGroup taxonomy (locked in D-RS-b)

```
Home        → Research Home (NEW)
Discover    → Daily Brief · Event Radar · SEPA Daily Core · Stock Screener · Option Discovery · Momentum Radar
Analyze     → IV Radar · GEX Intraday · Analysis Model · Order Sentiment · Multi-leg Flow · Forecast Sessions · Intraday Playbook
Validate    → Backtest · Contract Greeks · Risk Model
Data        → Stock Data Readiness · Stock Watchlist · Option Screener
```

**Removed subGroups**: Daily Core · Intraday · Structure · Selection · Lab (their items redistributed above)

### Verify

```bash
cd bifrost-trade-frontend
npm run lint && npm run build
# Manual: sidebar visually matches new grouping; every existing route still reachable
```

### Acceptance

- All 17 existing routes present in new grouping (no orphans)
- New Home entry appears at top
- Screenshot attached to Phase report

---

# Wave RS-B · Three New Labs

**Goal**: Fill three obvious gaps versus institutional option quant desks using only existing data.

**Duration**: 3–4 weeks · up to 3 agents in parallel (one per lab)

**Common pattern per lab** (2 sub-phases each):
1. `X-engine` — compute + persist to `features.*` table + CronJob YAML
2. `X-api-fe` — API route + FE page

Every FE page **must**:
- Use `PageShell` + `PageHeader`
- Include `ResearchContextBar` at top for symbol/date
- End with "Save as Hypothesis" button (from RS-A3) pre-filled with current view context

## Phase RS-B-VRP1 · IV-RV Spread Engine

- **Repo**: `bifrost-research`
- **Depends on**: — (only Wave A done for FE linkage, but engine itself is independent)
- **Can run in parallel with**: RS-B-Surface1, RS-B-OpEx1

### Goal

Compute Volatility Risk Premium daily: rolling 20d/60d/252d Realized Vol vs ATM IV → spread → percentile → forward return IC.

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/engines/vrp/__init__.py`
  - `bifrost-research/src/bifrost_research/engines/vrp/compute.py`
  - `bifrost-research/src/bifrost_research/engines/vrp/entry.py` (CronJob entrypoint)
  - `bifrost-research/tests/engines/test_vrp.py`
  - `bifrost-research/k8s/engines/cronjob-vrp.yaml`
- **modify**:
  - `bifrost-research/src/bifrost_research/schema/ddl.py` — add table below
  - `bifrost-research/docs/CAPABILITY_MATRIX.md`

### Data model

```sql
CREATE TABLE IF NOT EXISTS features.stock_signal_vrp_daily (
    symbol        text NOT NULL,
    trade_date    date NOT NULL,
    rv_20d        double precision,
    rv_60d        double precision,
    rv_252d       double precision,
    atm_iv_30d    double precision,
    vrp_20d       double precision,           -- atm_iv_30d - rv_20d (annualized)
    vrp_60d       double precision,
    vrp_pct_252d  double precision,           -- percentile of vrp_60d over trailing 252d
    fwd_ret_20d   double precision,           -- backfilled once 20d elapses (nullable)
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS vrp_pct ON features.stock_signal_vrp_daily(vrp_pct_252d) WHERE vrp_pct_252d IS NOT NULL;
```

### Inputs

- `raw_market.stock_daily` (for RV calc)
- `features.option_metric_iv_percentile` or `raw_market.option_snapshot` filtered to ~30d ATM (source of truth: reuse existing volatility engine helper)

### CronJob

- Schedule: `10 23 * * MON-FRI` UTC (after Polygon daily close and volatility engine)
- Concurrency: Forbid
- Image: reuse `bifrost-research` engines image
- Symbols: watchlist ∪ SPX/SPY/QQQ/IWM ∪ SEPA screener universe

### Verify

```bash
cd bifrost-research
make lint && make test
python -m bifrost_research.engines.vrp.entry --symbol NVDA --dry-run
```

### Acceptance

- Test coverage: RV formula matches reference (close-to-close log return std × √252)
- Dry-run prints one row of computed features
- CronJob YAML lints via `kubectl apply --dry-run=client -f k8s/engines/cronjob-vrp.yaml`

---

## Phase RS-B-VRP2 · VRP API + Lab Page

- **Repo**: `bifrost-research` + `bifrost-trade-frontend`
- **Depends on**: RS-B-VRP1 (table exists) + RS-A3 (Save-as button)
- **Can run in parallel with**: RS-B-Surface2, RS-B-OpEx2

### Goal

Serve VRP + build `/research/vrp-lab` page.

### Backend files

- **new**:
  - `bifrost-research/src/bifrost_research/api/vrp.py`
  - `bifrost-research/tests/api/test_vrp.py`
- **modify**:
  - `bifrost-research/src/bifrost_research/api/app.py`

### API contract

```
GET /research/vrp/latest?symbol=NVDA               -> single latest row
GET /research/vrp/history?symbol=NVDA&days=252     -> time series
GET /research/vrp/extremes?bucket=high&limit=20    -> screener (top-N high/low VRP percentile symbols)
```

### Frontend files

- **new**:
  - `bifrost-trade-frontend/src/pages/research/VrpLabPage.tsx`
  - `bifrost-trade-frontend/src/api/research/vrp.ts`
  - `bifrost-trade-frontend/src/hooks/useVrpData.ts`
- **modify**:
  - `src/lib/router.tsx` — add `/research/vrp-lab`
  - `src/layout/navConfig.ts` — add to `Analyze` group

### Page composition

- `PageHeader` title "IV-RV Spread Lab (VRP)"
- `ResearchContextBar` (symbol + date)
- Verdict Strip: "VRP percentile 85 — Sell vol edge significant" (with token `text-success/warning/danger`)
- Section 1: IV vs RV time series (SVG / recharts equivalent — reuse existing chart pattern from GEX/Terrain)
- Section 2: VRP percentile histogram
- Section 3: VRP Extremes table (top-20 high, top-20 low) with `SymbolPicker` cross-link
- Save-as-Hypothesis at bottom right

### Verify

```bash
cd bifrost-research && make test && make lint
cd bifrost-trade-frontend && npm run lint && npm run build && npm run check:legacy-css
```

### Acceptance

- Local Vite `:5173` `/research/vrp-lab?symbol=NVDA` renders chart + table
- Empty state graceful when no CronJob rows yet
- Save-as-Hypothesis pre-fills `origin_page="vrp-lab"` `origin_ref={symbol, date, vrp_pct}`

---

## Phase RS-B-Surface1 · Vol Surface Engine (SVI)

- **Repo**: `bifrost-research`
- **Depends on**: —
- **Can run in parallel with**: RS-B-VRP1, RS-B-OpEx1

### Goal

Fit SVI 5-parameter smile per (symbol, trade_date, expiry). Persist parameters + residuals.

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/engines/vol_surface/__init__.py`
  - `bifrost-research/src/bifrost_research/engines/vol_surface/svi.py` — pure-Python 5-parameter SVI (Gatheral raw): `w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))`
  - `bifrost-research/src/bifrost_research/engines/vol_surface/fit.py` — least-squares wrapper using scipy.optimize
  - `bifrost-research/src/bifrost_research/engines/vol_surface/entry.py`
  - `bifrost-research/tests/engines/test_vol_surface.py`
  - `bifrost-research/k8s/engines/cronjob-vol-surface.yaml`
- **modify**:
  - `bifrost-research/src/bifrost_research/schema/ddl.py`

### Data model

```sql
CREATE TABLE IF NOT EXISTS features.option_surface_fit_daily (
    symbol        text NOT NULL,
    trade_date    date NOT NULL,
    expiry        date NOT NULL,
    dte           integer NOT NULL,
    -- SVI raw params
    svi_a         double precision,
    svi_b         double precision,
    svi_rho       double precision,
    svi_m         double precision,
    svi_sigma     double precision,
    atm_vol       double precision,
    atm_slope     double precision,           -- ∂σ/∂k at k=0 (skew)
    fit_rmse      double precision,
    n_points      integer,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trade_date, expiry)
);

CREATE TABLE IF NOT EXISTS features.option_surface_residual_daily (
    symbol        text NOT NULL,
    trade_date    date NOT NULL,
    expiry        date NOT NULL,
    strike        double precision NOT NULL,
    log_moneyness double precision,           -- ln(K/F)
    iv_market     double precision,
    iv_fitted     double precision,
    residual      double precision,           -- iv_market - iv_fitted
    residual_z    double precision,           -- residual / rmse
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trade_date, expiry, strike)
);
CREATE INDEX IF NOT EXISTS surface_residual_z_abs
  ON features.option_surface_residual_daily(abs(residual_z) DESC);
```

### Inputs

- `raw_market.option_snapshot` (or `option_daily` if snapshot missing) — bid/ask/last IV per contract
- ATM forward from `raw_market.stock_daily` close

### Constraints

- Only fit smiles with `n_points >= 10` and DTE between 7 and 90 days for v1
- Handle degenerate cases (all-flat smile) → skip row, log

### CronJob

- Schedule: `20 23 * * MON-FRI` UTC
- Symbols: watchlist ∪ SEPA universe ∪ benchmarks

### Verify

```bash
cd bifrost-research
make lint && make test
python -c "from bifrost_research.engines.vol_surface.svi import svi_total_variance; print(svi_total_variance(0.0, 0.04, 0.4, -0.3, 0.0, 0.1))"
```

### Acceptance

- SVI arbitrage-free constraints checked: `b*(1+|rho|) < 4/(T)` per Gatheral (test asserts on synthetic smile)
- Fit RMSE < 0.01 on synthetic smooth smile
- Residual table has same row count as input strikes

---

## Phase RS-B-Surface2 · Vol Surface API + Lab Page

- **Depends on**: RS-B-Surface1 + RS-A3
- **Can run in parallel with**: RS-B-VRP2, RS-B-OpEx2

### Backend

- **new**: `bifrost-research/src/bifrost_research/api/vol_surface.py`
- **modify**: `api/app.py`

### API contract

```
GET /research/vol-surface/fit?symbol=NVDA&trade_date=2026-08-25
GET /research/vol-surface/term-structure?symbol=NVDA&trade_date=2026-08-25
GET /research/vol-surface/residuals?symbol=NVDA&trade_date=2026-08-25&expiry=2026-09-19
GET /research/vol-surface/skew-extremes?limit=20     -- symbols with 25-delta RR at 1y extreme
```

### Frontend

- **new**:
  - `src/pages/research/VolSurfaceLabPage.tsx`
  - `src/components/charts/VolSurface3DChart.tsx` (v1 can be a heatmap: rows=expiry, cols=strike, color=iv or residual_z)
  - `src/components/charts/TermStructureChart.tsx`
  - `src/api/research/volSurface.ts`
  - `src/hooks/useVolSurfaceData.ts`
- **modify**: `router.tsx`, `navConfig.ts` (Analyze group)

### Page composition

- Verdict Strip: "25-delta RR at 92% 1y percentile — extreme call skew" (color per severity)
- Section 1: Term Structure line chart (ATM vol vs DTE)
- Section 2: Residual heatmap (which strikes deviate most from fit)
- Section 3: Skew Extremes table
- Save-as-Hypothesis

### Verify

Same three-command trio; visual acceptance on local Vite.

---

## Phase RS-B-OpEx1 · Vanna/Charm/OpEx Engine

- **Repo**: `bifrost-research`
- **Depends on**: —
- **Can run in parallel with**: RS-B-VRP1, RS-B-Surface1

### Goal

Compute Vanna and Charm dealer exposures (extending existing GEX engine), plus OpEx cycle indicator.

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/engines/opex_cycle/__init__.py`
  - `bifrost-research/src/bifrost_research/engines/opex_cycle/vanna_charm.py`
  - `bifrost-research/src/bifrost_research/engines/opex_cycle/calendar.py` (US monthly OpEx = 3rd Friday)
  - `bifrost-research/src/bifrost_research/engines/opex_cycle/entry.py`
  - `bifrost-research/tests/engines/test_opex_cycle.py`
  - `bifrost-research/k8s/engines/cronjob-opex-cycle.yaml`
- **modify**:
  - `bifrost-research/src/bifrost_research/schema/ddl.py`

### Data model

```sql
CREATE TABLE IF NOT EXISTS features.option_metric_vanna_charm_daily (
    symbol        text NOT NULL,
    trade_date    date NOT NULL,
    spot          double precision,
    total_vanna   double precision,             -- Σ contracts × mult × vanna
    total_charm   double precision,             -- Σ contracts × mult × charm
    vanna_zero_strike   double precision,       -- strike where cumulative vanna crosses zero
    charm_zero_strike   double precision,
    dte_to_opex   integer,                      -- days to next 3rd-Friday OpEx
    is_opex_week  boolean,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trade_date)
);
```

### Constraints

- Reuse `bifrost_research.engines.gex` helpers for dealer positioning assumption
- Vanna, Charm formulas: Black-Scholes analytical derivatives (`py_vollib` if installed, else pure-Python)

### CronJob

- Schedule: `30 23 * * MON-FRI` UTC

### Verify

```bash
make lint && make test
python -c "from bifrost_research.engines.opex_cycle.calendar import next_opex_friday; from datetime import date; print(next_opex_friday(date(2026,8,25)))"
```

### Acceptance

- Test: `next_opex_friday(2026-08-25)` returns `2026-09-18` (3rd Friday of Sept)
- Vanna/Charm formulas match reference values on synthetic contract (tolerance 1e-6)

---

## Phase RS-B-OpEx2 · OpEx Cycle API + Lab Page

- **Depends on**: RS-B-OpEx1 + RS-A3
- **Can run in parallel with**: RS-B-VRP2, RS-B-Surface2

### Backend

- **new**: `bifrost-research/src/bifrost_research/api/opex_cycle.py`

### API contract

```
GET /research/opex-cycle/current?symbol=SPX      -> current Vanna/Charm + dte_to_opex
GET /research/opex-cycle/history?symbol=SPX&cycles=12  -> last 12 OpEx cycle summaries
GET /research/opex-cycle/pin-analysis?symbol=SPX&cycles=24   -> historical spot deviation from max-pain into OpEx close
```

### Frontend

- **new**:
  - `src/pages/research/OpExCycleLabPage.tsx`
  - `src/components/charts/VannaCharmMap.tsx`
  - `src/api/research/opexCycle.ts`
  - `src/hooks/useOpExCycle.ts`
- **modify**: `router.tsx`, `navConfig.ts`

### Page composition

- Verdict Strip: "3 days to Sep OpEx · Vanna positive · dealer short vol"
- Section 1: Vanna/Charm distribution across strikes (dual bars)
- Section 2: OpEx cycle timeline — historical spot vs max-pain distance into OpEx close (12 recent cycles)
- Section 3: Pin risk indicator + current max-pain distance
- Save-as-Hypothesis

### Verify

Same trio; visual acceptance.

---

# Wave RS-C · Backtest Incremental Upgrade

**Goal**: Grow current daily-settlement Backtest into an event-driven, realistic-fills, Hypothesis-linked engine — incrementally.

**Duration**: 4–5 weeks · 2 agents

## Phase RS-C1 · Event-Driven Query Engine

- **Repo**: `bifrost-research`
- **Depends on**: — (independent of A/B)
- **Can run in parallel with**: RS-C2

### Goal

Query historical performance of `(event, strategy_template)` pairs. Zero DDL — pure compute over existing `raw_market.option_daily` + `raw_market.stock_daily`.

### Event definitions

```python
class EventDef:
    kind: Literal["earnings", "opex", "sepa_hit", "iv_percentile_threshold", "sql"]
    params: dict           # e.g. {"symbols": ["NVDA"], "days_before": 5}
```

### Strategy templates (v1)

- `long_atm_straddle` (entry at open of D-N, exit at close of D+M)
- `short_atm_straddle`
- `long_atm_call` / `long_atm_put`
- `short_30d_iron_condor` (25-delta short strikes)
- `covered_call_1sd` (requires stock leg)

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/engines/backtest/event_query.py`
  - `bifrost-research/src/bifrost_research/engines/backtest/strategy_templates.py`
  - `bifrost-research/src/bifrost_research/engines/backtest/event_defs.py`
  - `bifrost-research/tests/engines/test_backtest_event_query.py`
- **modify**: — (no DDL yet)

### Verify

```bash
make lint && make test
python -c "
from bifrost_research.engines.backtest.event_query import run_event_query
from bifrost_research.engines.backtest.event_defs import EventDef
r = run_event_query(EventDef(kind='earnings', params={'symbols':['NVDA'], 'days_before':1, 'days_after':1}), 'long_atm_straddle', lookback_years=3)
print(r['summary'])
"
```

### Acceptance

- Function returns dict with keys `runs[]` (per-event) + `summary` (win_rate, avg_pnl, max_dd, sharpe)
- Test: on synthetic earnings dates → straddle P&L matches close-to-close computation

---

## Phase RS-C2 · Realistic Fill Model

- **Repo**: `bifrost-research`
- **Depends on**: —
- **Can run in parallel with**: RS-C1

### Goal

Replace close-price fills with `mid ± spread%` model + commission + multiplier.

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/engines/backtest/fills.py`
  - `bifrost-research/tests/engines/test_backtest_fills.py`
- **modify**:
  - `bifrost-research/src/bifrost_research/engines/backtest/settlement.py` — use `fills.compute_fill_price()` instead of raw close
  - `bifrost-research/src/bifrost_research/engines/backtest/strategy_templates.py` (from C1) — call fills helper

### Fill model

```python
def compute_fill_price(side: 'buy'|'sell', bid: float, ask: float, close: float, config: FillConfig) -> float:
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else close
    spread = max(ask - bid, 0.0)
    slippage = config.slippage_pct_of_spread * spread   # default 0.2
    if side == 'buy': return mid + slippage
    else:             return mid - slippage

class FillConfig:
    slippage_pct_of_spread: float = 0.2
    commission_per_contract: float = 0.65       # IB default
    multiplier: int = 100
    exercise_style: Literal['american_no_early','european'] = 'american_no_early'
```

### Verify

```bash
make lint && make test
```

### Acceptance

- Test: on synthetic (bid=1.0, ask=1.2, close=1.1) → buy fill = 1.1 + 0.04 = 1.14
- All existing backtest tests still pass (settlement.py refactor is backward-compatible via default config)

---

## Phase RS-C3 · Walk-Forward + Benchmark

- **Depends on**: RS-C2
- **Can run in parallel with**: RS-C4 (partial)

### Goal

- Rolling window optimization (annual refit)
- Compare each run to SPY buy-hold benchmark
- Compare each run to "same strategy zero-signal" (does the signal add value)

### Files

- **new**:
  - `bifrost-research/src/bifrost_research/engines/backtest/walk_forward.py`
  - `bifrost-research/src/bifrost_research/engines/backtest/benchmark.py`
  - `bifrost-research/tests/engines/test_walk_forward.py`
- **modify**: — (extends C1 API)

### Verify

Standard trio.

### Acceptance

- Walk-forward: given 5y history, 1y IS + 3M OOS windows → returns 12+ OOS periods with metrics
- Benchmark: SPY buy-hold Sharpe computed correctly on synthetic price series

---

## Phase RS-C4 · Event Query API + Backtest FE Upgrade + Hypothesis Linkage

- **Depends on**: RS-A2 (hypothesis API), RS-C1, RS-C2, RS-C3
- **Sign-off**: required (major FE change)

### Backend

- **new**:
  - `bifrost-research/src/bifrost_research/api/backtest_event.py`
- **modify**:
  - `api/app.py` — include router
  - `schema/ddl.py` — add `research.backtest_run` table:

```sql
CREATE TABLE IF NOT EXISTS research.backtest_run (
    id                text PRIMARY KEY,
    hypothesis_id     text REFERENCES research.hypothesis(id) ON DELETE SET NULL,
    event_def         jsonb NOT NULL,
    strategy_template text NOT NULL,
    fill_config       jsonb NOT NULL,
    lookback_years    integer NOT NULL,
    summary           jsonb NOT NULL,           -- win_rate, avg_pnl, sharpe, max_dd
    walk_forward      jsonb,
    benchmark         jsonb,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX backtest_run_hypothesis ON research.backtest_run(hypothesis_id);
```

### API contract

```
POST /research/backtest/event-query    body: {event_def, strategy_template, fill_config?, lookback_years?, hypothesis_id?}
                                        -> {run_id, summary, runs[], walk_forward?, benchmark?}
GET  /research/backtest/runs?hypothesis_id=...
GET  /research/backtest/run/{run_id}
```

### Frontend

- **new**:
  - `src/components/research/EventQueryBuilder.tsx` — form UI (event kind + params + strategy template + fill config sliders)
  - `src/components/research/BacktestRunResultCard.tsx`
  - `src/api/research/backtestEvent.ts`
  - `src/hooks/useBacktestEventQuery.ts`
- **modify**:
  - `src/pages/research/BacktestPage.tsx` — new "Event Query" tab (`SegmentControl` "Settlement · Event Query")
  - `src/pages/research/ResearchHomePage.tsx` (RS-A4) — Recent Backtests section now populated

### Page composition

BacktestPage → 2 tabs:
- **Settlement** (existing daily settlement view)
- **Event Query** (new; EventQueryBuilder → results card → "Attach to Hypothesis" button)

### Verify

- `make lint && make test` in `bifrost-research`
- `npm run lint && npm run build && npm run check:legacy-css` in `bifrost-trade-frontend`
- Manual: build a "NVDA earnings D-1 long straddle over 3 years" query end-to-end, save to Hypothesis, verify Hypothesis card shows the run in Home page

### Acceptance

- Event Query tab produces results within 5s for 3-year lookback on single symbol
- Attach-to-Hypothesis persists `linked_backtest_ids`
- ResearchHomePage Recent Backtests populated

---

# Wave RS-D · Research Infrastructure (Deferred)

**Not scheduled**. Revisit after Wave RS-C ships. Contains:

- Jupyter connector to Golden Source (Dockerfile + `docker-compose.research.yml`)
- MLflow tracking server (local) for Forecast Sessions
- Dagster stub → production (Postgres storage + webserver + daemon; migrate CronJobs to assets)
- dbt docs site route in Ops Console

Do **not** start Wave D until Owner explicitly authorizes; RS-C sign-off is the natural checkpoint.

---

# Cross-cutting Rules

## Every Phase must

1. Follow `.cursor/rules/phase-execution.mdc` — verify before reporting, no architectural inventions
2. Update `bifrost-research/docs/CAPABILITY_MATRIX.md` when adding a new engine / API
3. Update `bifrost-research/docs/RESEARCH_UX_DECISIONS.md` when locking a new D-RS-* decision
4. Update `bifrost-trade-frontend/docs/CAPABILITY_MATRIX.md` when adding a new page or FE hook
5. Respect D10 — no phase touches live order placement
6. Respect Dense UI system — reuse `data-display` primitives, no new `*.module.css` tables

## Version bumps

- `bifrost-research/pyproject.toml`:
  - After RS-A2: `0.9.0` (hypothesis API — public surface)
  - After RS-B-VRP2: `0.10.0`
  - After RS-B-Surface2: `0.11.0`
  - After RS-B-OpEx2: `0.12.0`
  - After RS-C4: `0.13.0`
- `bifrost-trade-frontend` has no SemVer; only CAPABILITY_MATRIX rows

## Reporting after each Phase

Follow the Phase Completion Report template in `phase-execution.mdc`. Attach screenshot for FE phases. Do not auto-continue to next Phase unless in Owner-authorized batch mode.

---

# Global QA (after Wave RS-C4 completes)

1. Reset session: log in as fresh user, open `/research`
2. Save 3 Hypotheses from 3 different Discovery pages
3. Attach 2 Backtest event-queries to one Hypothesis
4. Rotate Hypothesis to "validated" with conclusion; confirm it reflects on Home
5. Run all three new labs on NVDA / SPX / AAPL — spot-check Verdict strings against manual data
6. `check:legacy-css` still passes; `npm run build` size delta < 25% vs pre-RS baseline
7. `bifrost-research` `make test` full suite green
8. Update `bifrost-trade-infra/docs/MIGRATION_TRACKING.md` (optional) with a "Wave RS complete" note

---

# Sign-off Ladder

| Gate | Trigger | Owner action |
|------|---------|--------------|
| SO-1 | RS-A1 complete | Review DDL; approve `research.hypothesis` shape |
| SO-2 | RS-A2 complete | Review API contract; version bump to 0.9.0 |
| SO-3 | RS-A5 complete | Screenshot review of new sidebar; approve replace-not-preserve |
| SO-4 | RS-B (any lab) complete | Review Verdict phrasing per lab |
| SO-5 | RS-C4 complete | Full end-to-end demo; approve program done or continue to RS-D |
