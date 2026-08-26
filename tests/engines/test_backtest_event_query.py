"""Tests for the Event-Driven Query Engine — Wave RS-C1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest

from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.backtest.event_query import (
    run_event_query,
    summarize_runs,
)
from bifrost_research.engines.backtest.strategy_templates import (
    TEMPLATES,
    build_legs,
    covered_call_1sd,
    long_atm_straddle,
    short_30d_iron_condor,
)


# ---------------------------------------------------------------------------
# Fake DB scaffolding — Pattern B (see tests/engines/test_vrp.py)
# ---------------------------------------------------------------------------


@dataclass
class _StockBar:
    bar_date: date
    open: float
    close: float
    high: float
    low: float


@dataclass
class _OptionBar:
    underlying: str
    expiry: date
    strike: float
    option_right: str
    bar_date: date
    close: float
    high: float
    low: float
    open: float


@dataclass
class _FakeState:
    stock: dict[str, list[_StockBar]] = field(default_factory=dict)
    options: list[_OptionBar] = field(default_factory=list)
    corp_actions: list[tuple[str, str, date]] = field(default_factory=list)  # sym, type, ex_date
    event_radar: list[tuple[str, date, str]] = field(default_factory=list)  # sym, collected, text
    sepa_rows: list[tuple[str, date, float]] = field(default_factory=list)  # sym, td, score
    iv_rows: list[tuple[str, date, float]] = field(default_factory=list)    # sym, td, iv_pct


class _FakeCursor:
    def __init__(self, parent: "_FakeConn") -> None:
        self.parent = parent
        self._fetched: list[Any] = []

    def execute(self, query: str, params: Any = None) -> None:
        q = " ".join(query.split()).lower()
        self.parent.statements.append((q, params))
        state = self.parent.state

        if "from raw_market.corporate_action" in q:
            start, end, universe, _u2 = params
            uni_set = set(universe) if universe else None
            self._fetched = [
                (sym, ex_date)
                for (sym, kind, ex_date) in state.corp_actions
                if kind == "earnings"
                and start <= ex_date <= end
                and (uni_set is None or sym in uni_set)
            ]
            return

        if "from features.event_signal_radar_daily" in q:
            start, end = params
            found: list[tuple[str, date]] = []
            for sym, td, text in state.event_radar:
                if start <= td <= end and (
                    "earnings" in text.lower() or "财报" in text
                ):
                    found.append((sym, td))
            self._fetched = found
            return

        if "from features.stock_signal_sepa_daily" in q:
            start, end, threshold, *rest = list(params)
            sym_filter = set(rest[0]) if rest else None
            self._fetched = [
                (sym, td)
                for (sym, td, score) in state.sepa_rows
                if start <= td <= end and score >= threshold and (
                    sym_filter is None or sym in sym_filter
                )
            ]
            return

        if "from features.option_metric_iv_percentile_daily" in q:
            start, end, threshold, *rest = list(params)
            sym_filter = set(rest[0]) if rest else None
            op_above = ">=" in q
            hits: list[tuple[str, date]] = []
            for sym, td, iv in state.iv_rows:
                if not (start <= td <= end):
                    continue
                if sym_filter and sym not in sym_filter:
                    continue
                if op_above and iv >= threshold:
                    hits.append((sym, td))
                elif not op_above and iv <= threshold:
                    hits.append((sym, td))
            self._fetched = hits
            return

        if "from raw_market.stock_daily" in q and "between" in q:
            sym, start, end = params
            bars = state.stock.get(sym.upper(), [])
            self._fetched = [
                (b.bar_date, b.high, b.low, b.close) for b in bars if start <= b.bar_date <= end
            ]
            return

        if "from raw_market.stock_daily" in q:
            sym, on_or_before = params
            bars = [b for b in state.stock.get(sym.upper(), []) if b.bar_date <= on_or_before]
            if not bars:
                self._fetched = []
                return
            bars.sort(key=lambda b: b.bar_date, reverse=True)
            b = bars[0]
            self._fetched = [(b.bar_date, b.open, b.close)]
            return

        if "from raw_market.option_daily" in q:
            sym, right, on_or_before, _same = params
            rows: list[Any] = []
            for opt in state.options:
                if opt.underlying != sym.upper():
                    continue
                if opt.option_right != right:
                    continue
                if opt.bar_date > on_or_before:
                    continue
                if opt.expiry <= on_or_before:
                    continue
                rows.append(
                    (
                        opt.expiry,
                        opt.strike,
                        opt.close,
                        opt.open,
                        opt.high,
                        opt.low,
                        opt.bar_date,
                        f"{opt.underlying}-{opt.expiry.isoformat()}-{opt.option_right}-{opt.strike}",
                    )
                )
            rows.sort(key=lambda r: r[6], reverse=True)
            self._fetched = rows[:400]
            return

        self._fetched = []

    def fetchall(self) -> list[Any]:
        return list(self._fetched)

    def fetchone(self) -> Any:
        return self._fetched[0] if self._fetched else None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeConn:
    def __init__(self, state: _FakeState | None = None) -> None:
        self.state = state or _FakeState()
        self.statements: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:  # pragma: no cover
        return None

    def rollback(self) -> None:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _synth_stock_bars(sym: str, start: date, end: date, spot0: float = 100.0) -> list[_StockBar]:
    bars: list[_StockBar] = []
    d = start
    price = spot0
    step = (end - start).days + 1
    for i in range(step):
        # Gentle deterministic upward drift + wiggle
        price *= 1.0 + 0.001 * ((i * 13 + hash(sym)) % 11 - 5) / 10.0
        bars.append(
            _StockBar(
                bar_date=d,
                open=price,
                close=price,
                high=price * 1.01,
                low=price * 0.99,
            )
        )
        d = d + timedelta(days=1)
    return bars


def _add_option_series(
    state: _FakeState,
    underlying: str,
    expiry: date,
    strike: float,
    right: str,
    days: list[date],
    prices: list[float],
) -> None:
    for d, p in zip(days, prices):
        state.options.append(
            _OptionBar(
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_right=right,
                bar_date=d,
                close=p,
                high=p * 1.02,
                low=p * 0.98,
                open=p,
            )
        )


# ---------------------------------------------------------------------------
# EventDef basic validation
# ---------------------------------------------------------------------------


def test_event_def_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        EventDef(kind="unknown_kind", params={})  # type: ignore[arg-type]


def test_event_def_roundtrip() -> None:
    e = EventDef(kind="opex", params={"symbols": ["SPY"]})
    payload = e.to_dict()
    assert payload["kind"] == "opex"
    e2 = EventDef.from_dict(payload)
    assert e2 == e


# ---------------------------------------------------------------------------
# Strategy templates smoke
# ---------------------------------------------------------------------------


def test_templates_registry_lists_six_v1_templates() -> None:
    assert set(TEMPLATES) >= {
        "long_atm_straddle",
        "short_atm_straddle",
        "long_atm_call",
        "long_atm_put",
        "short_30d_iron_condor",
        "covered_call_1sd",
    }


def test_long_atm_straddle_has_two_legs() -> None:
    legs = long_atm_straddle(entry_offset_days=-1, exit_offset_days=1)
    assert len(legs) == 2
    assert {leg.option_right for leg in legs} == {"C", "P"}
    assert all(leg.side == "buy" for leg in legs)


def test_short_30d_iron_condor_has_four_legs() -> None:
    legs = short_30d_iron_condor(short_delta=0.25)
    assert len(legs) == 4
    kinds = [(leg.side, leg.option_right) for leg in legs]
    assert ("sell", "C") in kinds
    assert ("buy", "C") in kinds
    assert ("sell", "P") in kinds
    assert ("buy", "P") in kinds


def test_covered_call_has_stock_and_option() -> None:
    legs = covered_call_1sd(quantity=1)
    kinds = {leg.kind for leg in legs}
    assert kinds == {"stock", "option"}
    stock = next(leg for leg in legs if leg.kind == "stock")
    assert stock.quantity == 100


def test_build_legs_unknown_raises() -> None:
    with pytest.raises(ValueError):
        build_legs("does_not_exist")


# ---------------------------------------------------------------------------
# End-to-end: straddle on synthetic earnings dates
# ---------------------------------------------------------------------------


def _build_state_with_earnings_and_options(event_dates: list[date], symbol: str = "NVDA") -> _FakeState:
    state = _FakeState()
    # Stock bars: cover +/- 40 days around each event, spot ~100
    first = min(event_dates) - timedelta(days=40)
    last = max(event_dates) + timedelta(days=40)
    state.stock[symbol] = _synth_stock_bars(symbol, first, last, spot0=100.0)

    # Register earnings via corporate_action (highest-priority path)
    for d in event_dates:
        state.corp_actions.append((symbol, "earnings", d))

    # Options: ATM strike ≈ 100 for each event's target expiry (~30d out)
    for d in event_dates:
        expiry = d + timedelta(days=30)
        # entry day (d-1) call closes 2.0; exit day (d+1) call closes 3.5 → +1.5 per contract
        entry_day = d - timedelta(days=1)
        exit_day = d + timedelta(days=1)
        _add_option_series(
            state,
            symbol,
            expiry=expiry,
            strike=100.0,
            right="C",
            days=[entry_day, exit_day],
            prices=[2.0, 3.5],
        )
        # put entry 2.5, exit 1.0 → -1.5
        _add_option_series(
            state,
            symbol,
            expiry=expiry,
            strike=100.0,
            right="P",
            days=[entry_day, exit_day],
            prices=[2.5, 1.0],
        )
    return state


def test_run_event_query_earnings_long_straddle_matches_close_to_close() -> None:
    today = date(2026, 6, 1)
    events = [today - timedelta(days=91 * i) for i in (1, 2)]
    state = _build_state_with_earnings_and_options(events, symbol="NVDA")
    conn = _FakeConn(state)

    result = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["NVDA"]}),
        template_name="long_atm_straddle",
        lookback_years=1,
        conn=conn,
        today=today,
        entry_offset_days=-1,
        exit_offset_days=1,
        target_dte=30,
    )

    assert result["event_source"] == "corporate_action"
    assert result["summary"]["n_events"] == len(events)
    # Per event: call (+1.5) + put (-1.5) → 0. With multiplier 100 → 0.
    for run in result["runs"]:
        assert len(run["legs"]) == 2
        assert run["pnl"] == 0.0

    # Aggregate summary sanity
    assert result["summary"]["avg_pnl"] == 0.0
    assert 0.0 <= result["summary"]["win_rate"] <= 1.0
    assert result["advisory"].startswith("D10")


def test_run_event_query_earnings_long_call_positive_pnl_x100_multiplier() -> None:
    today = date(2026, 6, 1)
    events = [today - timedelta(days=45)]
    state = _build_state_with_earnings_and_options(events, symbol="NVDA")
    conn = _FakeConn(state)

    result = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["NVDA"]}),
        template_name="long_atm_call",
        lookback_years=1,
        conn=conn,
        today=today,
    )
    assert result["summary"]["n_events"] == 1
    run = result["runs"][0]
    # Call: entry 2.0 → exit 3.5, sign=+1, qty=1, mult=100 (default when no fill_config)
    # NOTE: RS-C1 default (no fill_config) means multiplier is 100 in leg pricing.
    assert run["pnl"] == pytest.approx((3.5 - 2.0) * 100.0, rel=1e-6)


# ---------------------------------------------------------------------------
# MFE / MAE bounds
# ---------------------------------------------------------------------------


def test_mfe_nonnegative_and_mae_nonpositive() -> None:
    today = date(2026, 6, 1)
    events = [today - timedelta(days=45)]
    state = _build_state_with_earnings_and_options(events, symbol="NVDA")
    conn = _FakeConn(state)
    result = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["NVDA"]}),
        template_name="long_atm_straddle",
        lookback_years=1,
        conn=conn,
        today=today,
    )
    for run in result["runs"]:
        assert run["mfe"] >= 0.0
        assert run["mae"] <= 0.0


# ---------------------------------------------------------------------------
# Event resolvers — opex / iv / sepa
# ---------------------------------------------------------------------------


def test_opex_resolver_returns_third_fridays() -> None:
    today = date(2026, 12, 20)
    conn = _FakeConn()
    result = run_event_query(
        EventDef(kind="opex", params={"symbols": ["SPY"]}),
        template_name="long_atm_call",
        lookback_years=1,
        conn=conn,
        today=today,
    )
    # Regardless of whether the option data is present (runs will be 0),
    # event_source must be opex and no crash.
    assert result["event_source"] == "opex"
    # No stock/option data → all runs skipped
    assert result["summary"]["n_events"] == 0


def test_sepa_hit_resolver_uses_threshold() -> None:
    today = date(2026, 6, 1)
    state = _FakeState()
    state.sepa_rows = [
        ("NVDA", today - timedelta(days=10), 0.85),
        ("NVDA", today - timedelta(days=20), 0.60),  # below threshold
        ("AAPL", today - timedelta(days=15), 0.90),
    ]
    conn = _FakeConn(state)
    result = run_event_query(
        EventDef(kind="sepa_hit", params={"threshold": 0.7}),
        template_name="long_atm_call",
        lookback_years=1,
        conn=conn,
        today=today,
    )
    assert result["event_source"] == "sepa"
    # No option data → all runs skipped, but resolver saw 2 events
    assert result["skipped_events"] == 2


def test_iv_percentile_resolver_above() -> None:
    today = date(2026, 6, 1)
    state = _FakeState()
    state.iv_rows = [
        ("NVDA", today - timedelta(days=5), 85.0),
        ("NVDA", today - timedelta(days=6), 50.0),  # below
        ("SPY", today - timedelta(days=3), 92.0),
    ]
    conn = _FakeConn(state)
    result = run_event_query(
        EventDef(
            kind="iv_percentile_threshold",
            params={"threshold": 80.0, "direction": "above"},
        ),
        template_name="short_atm_straddle",
        lookback_years=1,
        conn=conn,
        today=today,
    )
    assert result["event_source"] == "iv"
    assert result["skipped_events"] == 2  # 2 hits, no option data → skipped


def test_earnings_stub_fallback_when_no_data() -> None:
    today = date(2026, 6, 1)
    conn = _FakeConn()  # empty state
    result = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["NVDA"]}),
        template_name="long_atm_call",
        lookback_years=1,
        conn=conn,
        today=today,
    )
    assert result["event_source"] == "stub"
    assert "stub" in result["event_source_notes"].lower()


def test_sql_kind_not_implemented() -> None:
    today = date(2026, 6, 1)
    conn = _FakeConn()
    with pytest.raises(NotImplementedError):
        run_event_query(
            EventDef(kind="sql", params={"query": "SELECT 1"}),
            template_name="long_atm_call",
            lookback_years=1,
            conn=conn,
            today=today,
        )


# ---------------------------------------------------------------------------
# summarize_runs public helper
# ---------------------------------------------------------------------------


def test_summarize_runs_from_dicts() -> None:
    runs = [
        {"event_date": "2026-01-01", "symbol": "NVDA", "pnl": 1.0, "mfe": 0.02, "mae": -0.01, "legs": []},
        {"event_date": "2026-02-01", "symbol": "NVDA", "pnl": -0.5, "mfe": 0.01, "mae": -0.03, "legs": []},
        {"event_date": "2026-03-01", "symbol": "NVDA", "pnl": 2.0, "mfe": 0.03, "mae": -0.005, "legs": []},
    ]
    s = summarize_runs(runs)
    assert s["n_events"] == 3
    assert 0.0 <= s["win_rate"] <= 1.0
    assert s["max_drawdown"] <= 0.0
