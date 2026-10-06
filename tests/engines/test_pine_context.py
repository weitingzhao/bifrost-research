"""Option context for Pine scripts (S6, engines/pine/context.py)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from bifrost_research.engines.pine import build, client, context
from bifrost_research.engines.pine.library import PineScript, validate

D0 = date(2025, 1, 6)  # a Monday


def _sessions(n: int, start: date = D0) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _pine(body: str) -> str:
    return f'//@version=5\nindicator("t")\n{body}\nplotshape(close > 0, "buy")'


# -- which series a script reads -------------------------------------------------------------


def test_referenced_names_in_catalog_order() -> None:
    src = _pine(
        'v = request.security("VRP_20", timeframe.period, close)\n'
        'i = request.security(symbol = "IV_30", timeframe = "1D", expression = ta.sma(close, 5))\n'
        'o = request.security(syminfo.tickerid, timeframe.period, close)\n'
        "s = request.security('SPY', 'D', close)"
    )
    assert context.referenced(src) == ["IV_30", "VRP_20", "SPY"]
    assert context.referenced(_pine("x = close")) == []


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('x = request.security("IV_RNK", timeframe.period, close)', r'unknown series "IV_RNK"; available: IV_30'),
        ('x = request.security("IV:30", timeframe.period, close)', "unknown series"),
        ("x = request.security(sym, timeframe.period, close)", "series name in quotes"),
        ('x = request.security("IV_30", "W", close)', "own timeframe only"),
        ("x = request.security(syminfo.tickerid, tf, close)", "own timeframe only"),
        ('x = request.security("IV_30", timeframe.period, close, lookahead = barmerge.lookahead_on)', "lookahead_on"),
        ('x = request.financial("AAPL", "EPS", "FQ")', "only request.security"),
    ],
)
def test_refused_calls(body: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        context.referenced(_pine(body))
    with pytest.raises(ValueError, match=match):
        validate("x_script", "x", _pine(body))


def test_the_catalog_has_no_colon_and_no_bare_ticker_lookalikes() -> None:
    for name, s in context.CATALOG.items():
        assert ":" not in name and name == name.upper()
        assert "_" in name or s.kind == context.MARKET  # SPY is the one real ticker, served as SPY's bars
    assert {s.name for s in context.CATALOG.values() if s.kind == context.MARKET} == {"SPY", "SPY_IV_30", "SPY_IV_RANK"}
    # Not served (Owner 2026-10-06): under two months of history.
    assert not any(n.startswith(("GEX", "SKEW", "ZERO", "WALL")) for n in context.CATALOG)


# -- alignment and warm-up -------------------------------------------------------------------


def test_align_carries_at_most_five_sessions_then_na() -> None:
    s = _sessions(12)
    values = {s[1]: 10.0, s[2]: None, s[9]: 20.0}
    got = context.align(values, s)
    assert got[s[0]] is None  # before the series starts
    assert [got[d] for d in s[1:7]] == [10.0] * 6  # s[2] null, s[3..6] missing: carried five sessions
    assert [got[d] for d in s[7:9]] == [None, None]
    assert [got[d] for d in s[9:]] == [20.0, 20.0, 20.0]


def test_warm_from_is_the_latest_first_value_plus_the_warm_up() -> None:
    s = _sessions(300)
    a = {d: 1.0 for d in s[5:]}
    b = {d: 1.0 for d in s[40:]}
    assert context.warm_from([a, b], s) == s[140]
    assert context.warm_from([a], s, warmup=10) == s[15]
    assert context.warm_from([a, {}], s) is None  # a series that never starts: nothing is stored
    assert context.warm_from([{d: 1.0 for d in s[250:]}], s) is None  # starts too late for this window


# -- term structure ------------------------------------------------------------------------


def test_term_30_60_needs_a_back_expiry_and_reads_contango_as_positive() -> None:
    d = date(2025, 6, 2)
    e = lambda n: d + timedelta(days=n)  # noqa: E731
    # 30 DTE at 0.30, 65 DTE at 0.335: 60-day leg interpolated between 44 and 65.
    got = context.term_30_60(d, [(e(16), 0.29), (e(30), 0.30), (e(44), 0.32), (e(65), 0.335)])
    assert got == pytest.approx((0.32 + (0.335 - 0.32) * 16 / 21 - 0.30) * 100)
    assert got > 0
    # Only front months: no back leg, not a zero slope.
    assert context.term_30_60(d, [(e(9), 0.4), (e(16), 0.38), (e(30), 0.36), (e(44), 0.35)]) is None
    # Backwardation reads negative.
    assert context.term_30_60(d, [(e(30), 0.50), (e(79), 0.40)]) < 0


# -- earnings, point in time ---------------------------------------------------------------


def test_earnings_counts_use_only_the_filings_on_file_that_day() -> None:
    days = context.open_days(date(2024, 1, 1), date(2026, 12, 31), frozenset())
    # Quarterly prints, then a non-results 2.02 (a delivery report) ten days before a release.
    prints = [date(2024, 7, 25), date(2024, 10, 24), date(2025, 1, 23), date(2025, 4, 24), date(2025, 7, 24)]
    filings = [(p, True) for p in prints] + [(date(2025, 10, 2), False), (date(2025, 10, 23), True)]
    sessions = [d for d in days if date(2025, 7, 1) <= d <= date(2025, 11, 28)]
    last, nxt = context.earnings_counts(filings, sessions, days)
    # Before the 07-24 print: last was 04-24; next by the rule = 2024-07-25 + 364 = 2025-07-24.
    assert nxt[date(2025, 7, 1)] == context._count_sessions(date(2025, 7, 1), date(2025, 7, 24), days)
    # On a print day that print is known; the next one is the following quarter (2024-10-24 + 364).
    assert last[date(2025, 7, 24)] == 0.0
    assert nxt[date(2025, 7, 24)] == context._count_sessions(date(2025, 7, 24), date(2025, 10, 23), days)
    # 10-02: the delivery report is the latest filing and nothing yet sets it aside: it reads as a print.
    assert last[date(2025, 10, 2)] == 0.0
    # 10-23: the release arrives; the 10-02 filing is set aside from then on, and 10-23 is the print.
    assert last[date(2025, 10, 23)] == 0.0
    assert last[date(2025, 10, 22)] == context._count_sessions(date(2025, 10, 2), date(2025, 10, 22), days)
    # Each session's value is the same whether or not later filings exist.
    for d in sessions:
        known = [f for f in filings if f[0] <= d]
        a, b = context.earnings_counts(known, [d], days)
        assert (a[d], b[d]) == (last[d], nxt[d]), d


def test_earn_next_falls_back_to_a_quarter_and_goes_stale() -> None:
    days = context.open_days(date(2025, 1, 1), date(2026, 6, 30), frozenset())
    filings = [(date(2025, 2, 3), True)]  # one print: the 52-week rule has nothing
    s = [date(2025, 3, 3), date(2025, 5, 5), date(2025, 5, 16), date(2025, 5, 20)]
    last, nxt = context.earnings_counts(filings, s, days)
    target = date(2025, 2, 3) + timedelta(days=context.EARN_FALLBACK_DAYS)  # 2025-05-05
    assert nxt[s[0]] == context._count_sessions(s[0], target, days)
    assert nxt[s[1]] == 0.0  # due today
    assert nxt[s[2]] == 0.0  # 11 days overdue, no print on file yet
    assert nxt[s[3]] is None  # 15 days overdue: the estimate means nothing any more
    assert last[s[0]] == context._count_sessions(date(2025, 2, 3), s[0], days)
    none_last, none_next = context.earnings_counts([], s, days)
    assert set(none_last.values()) == {None} and set(none_next.values()) == {None}


# -- load ------------------------------------------------------------------------------------


class _Conn:
    """Answers each reader's SELECT by the table it names."""

    def __init__(self, tables: dict[str, list[tuple[Any, ...]]]) -> None:
        self.tables, self.sql = tables, []
        self._rows: list[tuple[Any, ...]] = []

    def cursor(self) -> "_Conn":
        return self

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append(" ".join(sql.split()))
        self._rows = next((rows for t, rows in self.tables.items() if t in sql), [])

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


def test_load_aligns_converts_units_and_warms_up(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _sessions(160)
    bars = {"AAA": [{"date": d, "close": 10.0} for d in s], "BBB": [{"date": d, "close": 5.0} for d in s[50:]]}
    iv_rows = [("AAA", d, 0.25, 40.0, 55.0) for d in s[3:]] + [("SPY", d, 0.12, 10.0, 20.0) for d in s]
    vrp_rows = [("AAA", d, 0.02, -0.01, 60.0) for i, d in enumerate(s) if i % 30 != 7]
    spy = [{"date": d, "open": 1.0, "high": 1.0, "low": 1.0, "close": 400.0, "volume": 1.0} for d in s]
    monkeypatch.setattr(build, "load_bars_many", lambda conn, syms, a, b: {"SPY": spy})
    conn = _Conn({"option_metric_iv_percentile_daily": iv_rows, "stock_signal_vrp_daily": vrp_rows})
    per, market, warm = context.load(conn, ["IV_30", "VRP_20", "SPY", "SPY_IV_RANK"], bars)
    a = per["AAA"]
    assert a["IV_30"][s[2]] is None and a["IV_30"][s[3]] == pytest.approx(25.0)  # vol points
    assert a["VRP_20"][s[7]] == pytest.approx(2.0)  # missing day carried
    assert set(per["BBB"]["IV_30"].values()) == {None}  # no IV for BBB
    assert market["SPY"] == spy and market["SPY_IV_RANK"][s[0]] == 10.0
    assert warm["AAA"] == s[103]  # IV_30 starts at s[3]
    assert warm["BBB"] is None
    assert sum("atm_iv_daily" in q or "sec_8k" in q for q in conn.sql) == 0  # only the tables asked for
    with pytest.raises(ValueError, match="unknown series"):
        context.load(conn, ["GEX_REGIME"], bars)


# -- client, build ---------------------------------------------------------------------------


def test_client_sends_context_per_symbol_and_market_once(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_post(path, payload, timeout):  # noqa: ANN001
        seen.update(payload)
        return {"results": [{"symbol": "AAA", "buy": [], "sell": []}]}

    monkeypatch.setattr(client, "_post", fake_post)
    d1, d2 = date(2025, 1, 6), date(2025, 1, 7)
    bars = [{"date": d1, "close": 1.0}, {"date": d2, "close": 2.0}]
    client.run(
        "src",
        {"AAA": bars},
        context={"AAA": {"IV_30": {d2: 30.0, d1: None}}},
        market={"SPY": bars, "SPY_IV_30": {d1: 12.0}},
    )
    ms = lambda d: client._ms(d)  # noqa: E731
    assert seen["series"][0]["context"] == {"IV_30": [[ms(d1), None], [ms(d2), 30.0]]}
    assert seen["market"]["SPY_IV_30"] == [[ms(d1), 12.0]]
    assert seen["market"]["SPY"][1] == {"t": ms(d2), "o": 2.0, "h": 2.0, "l": 2.0, "c": 2.0, "v": 0}
    seen.clear()
    client.run("src", {"AAA": bars})
    assert "market" not in seen and "context" not in seen["series"][0]


def test_build_sends_context_and_holds_signals_to_the_context_warm_up(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _sessions(400, date(2024, 1, 1))
    bars = {"AAA": [{"date": d, "close": 10.0} for d in s]}
    plain = PineScript(id="plain", name="p", source=_pine("x = close"), version=1)
    ctx = PineScript(id="ctx", name="c", source=_pine('v = request.security("VRP_20", timeframe.period, close)'), version=1)
    calls: list[dict[str, Any]] = []

    def run(source, series, **kw):  # noqa: ANN001
        calls.append(kw)
        return {"AAA": {"buy": [s[150], s[250], s[350]], "sell": []}}

    written: dict[str, list[Any]] = {}
    monkeypatch.setattr(build, "connect", lambda: _Conn({}))
    monkeypatch.setattr(build, "ensure_builtins", lambda conn: 0)
    monkeypatch.setattr(build, "list_scripts", lambda conn, active_only: [plain, ctx])
    monkeypatch.setattr(build, "load_symbols_from_env_or_query", lambda conn, symbols: ["AAA"])
    monkeypatch.setattr(build, "built_versions", lambda conn: {"plain": 1, "ctx": 1})
    monkeypatch.setattr(build, "load_bars_many", lambda conn, chunk, a, b: bars)
    monkeypatch.setattr(build.client, "run", run)
    monkeypatch.setattr(build, "_write", lambda conn, sid, rows, **kw: written.setdefault(sid, []).extend(rows))
    monkeypatch.setattr(
        context, "load", lambda conn, names, b: ({"AAA": {"VRP_20": {}}}, {}, {"AAA": s[240]})
    )
    out = build.run(as_of=s[-1], full=True)
    assert calls[0] == {}  # a script without request.security: called exactly as before
    assert calls[1] == {"context": {"AAA": {"VRP_20": {}}}, "market": {}}
    assert [r[2] for r in written["plain"]] == [s[150], s[250], s[350]]
    assert [r[2] for r in written["ctx"]] == [s[250], s[350]]  # s[150] is before the context warm-up
    assert out["scripts"]["ctx"]["context"] == ["VRP_20"] and out["scripts"]["plain"]["context"] == []


def test_build_reports_a_script_with_an_unknown_series_and_runs_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = PineScript(id="bad", name="b", source=_pine('v = request.security("NOPE_1", timeframe.period, close)'))
    monkeypatch.setattr(build, "connect", lambda: _Conn({}))
    monkeypatch.setattr(build, "ensure_builtins", lambda conn: 0)
    monkeypatch.setattr(build, "list_scripts", lambda conn, active_only: [bad])
    monkeypatch.setattr(build, "load_symbols_from_env_or_query", lambda conn, symbols: ["AAA"])
    monkeypatch.setattr(build, "built_versions", lambda conn: {})
    out = build.run(as_of=D0)
    assert out["scripts"]["bad"]["errors"] == 1
    assert "unknown series" in out["scripts"]["bad"]["error_sample"]["*"]
