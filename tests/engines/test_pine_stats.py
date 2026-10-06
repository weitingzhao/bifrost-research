"""Pine signal statistics, 0.175.0 method (engines/pine/stats.py)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from bifrost_research.engines import signal_stats
from bifrost_research.engines.pine import stats
from bifrost_research.repositories import listing_lineage

MON = date(2026, 3, 2)


def _cal(n: int) -> list[date]:
    out, d = [], MON
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_dedupe_keeps_a_signal_only_h_sessions_after_the_last_one_kept() -> None:
    cal = _cal(30)
    sigs = [cal[0], cal[2], cal[4], cal[5], cal[11]]
    assert stats.dedupe(sigs, cal, 5) == [cal[0], cal[5], cal[11]]
    assert stats.dedupe(sigs, cal, 1) == sigs
    # A run of daily signals counts once per window.
    assert stats.dedupe(cal[:10], cal, 5) == [cal[0], cal[5]]


class _Conn:
    """Answers the three reads ``signal_stats`` makes: signals, SPY calendar, the aggregate."""

    def __init__(self, signals: list[tuple[str, date]], rows: list[tuple], calendar: list[date]) -> None:
        self.signals, self.rows, self.calendar = signals, rows, calendar
        self.params: dict[str, Any] = {}
        self._out: list[tuple] = []

    def cursor(self) -> "_Conn":
        return self

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def rollback(self) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if "stock_signal_pine_daily" in sql:
            self._out = list(self.signals)
        elif "symbol = 'SPY'" in sql:
            self._out = [(d,) for d in self.calendar]
        elif "raw_market.ticker" in sql:
            self._out = []
        else:
            self.params = params
            self._out = list(self.rows)

    def fetchall(self) -> list[tuple]:
        return self._out


def _row(kind: str, sym: str, d: date | None, n: int, s: float, w: int, g: int, t: int, x: int = 0) -> tuple:
    return (kind, sym, d, n, s, w, g, t, x)


def _run(conn: _Conn, **kw: Any) -> dict[str, Any]:
    args = dict(
        script="supertrend",
        side="buy",
        symbols=[],
        start=MON,
        end=MON + timedelta(days=60),
        horizons=[5],
        move_threshold=0.02,
        cost_bps=10.0,
        today=MON + timedelta(days=200),
    )
    args.update(kw)
    return stats.signal_stats(conn, **args)


def test_overlapping_signals_count_once_and_returns_are_net_and_directional() -> None:
    cal = _cal(40)
    # Three buy signals two sessions apart: only the first is outside the others' window.
    sigs = [("AAA", cal[0]), ("AAA", cal[2]), ("AAA", cal[4])]
    rows = [
        _row("s", "AAA", cal[0], 1, 0.05, 1, 1, 1),
        _row("s", "AAA", cal[2], 1, -0.01, 0, 0, 0),
        _row("s", "AAA", cal[4], 1, 0.03, 1, 1, 1),
        _row("b", "AAA", None, 20, 0.10, 11, 12, 3),
    ]
    out = _run(_Conn(sigs, rows, cal))
    h = out["by_horizon"]["5"]
    assert out["signals"] == 3 and h["n_raw"] == 3 and h["signal"]["n"] == 1
    assert h["signal"]["avg_return_gross"] == pytest.approx(0.05)
    assert h["signal"]["avg_return"] == pytest.approx(0.05 - 0.002)  # 10 bps each way
    assert h["baseline"]["avg_return"] == pytest.approx(0.10 / 20 - 0.002)
    assert h["avg_return_edge"] == pytest.approx(0.05 - 0.005, abs=1e-5)
    assert h["win_rate_edge"] == pytest.approx(1.0 - 11 / 20)
    assert h["sample_note"] == "noise" and out["sample_note"] == "noise"
    assert out["method"]["version"] == 2 and out["method"]["entry"] == "next_open"


def test_a_sell_signal_reads_a_fall_as_a_gain() -> None:
    cal = _cal(20)
    rows = [_row("s", "AAA", cal[0], 1, -0.04, 1, 1, 1), _row("b", "AAA", None, 10, 0.02, 4, 5, 0)]
    out = _run(_Conn([("AAA", cal[0])], rows, cal), side="sell")
    sig = out["by_horizon"]["5"]["signal"]
    assert sig["avg_return_gross"] == pytest.approx(0.04)
    assert sig["avg_return"] == pytest.approx(0.038)
    assert out["by_horizon"]["5"]["baseline"]["avg_return_gross"] == pytest.approx(-0.002)


def test_cluster_bootstrap_interval_and_equal_weight_baseline() -> None:
    cal = _cal(200)
    sigs, rows = [], []
    for i, sym in enumerate(["A", "B", "C", "D", "E", "F"]):
        for j in range(5):
            d = cal[10 * j + i]
            sigs.append((sym, d))
            up = (i + j) % 3 != 0
            rows.append(_row("s", sym, d, 1, 0.03 if up else -0.02, int(up), int(up), int(up)))
        # One big name with a weak baseline, five small ones with a strong one.
        rows.append(_row("b", sym, None, 1000 if sym == "A" else 10, (-0.01 * 1000) if sym == "A" else 0.05, 400 if sym == "A" else 7, 400 if sym == "A" else 7, 0))
    out = _run(_Conn(sigs, rows, cal), end=cal[-1])
    h = out["by_horizon"]["5"]
    assert h["signal"]["n"] == 30 and h["clusters"] == 6 and h["ci_method"] == "cluster_bootstrap_symbol"
    lo, hi = h["ci90"]["avg_return_edge"]
    assert lo <= h["avg_return_edge"] <= hi
    lo, hi = h["ci90"]["win_rate"]
    assert lo <= h["signal"]["win_rate"] <= hi
    # Pooled baseline is dominated by A; the equal-weight one is not.
    assert h["baseline"]["win_rate"] == pytest.approx(435 / 1050, abs=1e-4)
    assert h["baseline_equal_weight"]["win_rate"] == pytest.approx((0.4 + 5 * 0.7) / 6, abs=1e-4)
    assert h["baseline_equal_weight"]["symbols"] == 6
    # Same seed, same interval.
    again = _run(_Conn(sigs, rows, cal), end=cal[-1])["by_horizon"]["5"]["ci90"]
    assert again == h["ci90"]


def test_few_names_fall_back_to_signal_resampling_and_tiny_samples_get_no_interval() -> None:
    cal = _cal(100)
    sigs = [("AAA", cal[i * 6]) for i in range(8)]
    rows = [_row("s", "AAA", d, 1, 0.01 * (i % 3 - 1), int(i % 3 == 2), int(i % 3 == 2), 0) for i, (_s, d) in enumerate(sigs)]
    rows.append(_row("b", "AAA", None, 50, 0.0, 25, 25, 0))
    h = _run(_Conn(sigs, rows, cal), end=cal[-1])["by_horizon"]["5"]
    assert h["ci_method"] == "iid_signal" and h["ci90"]["win_rate"] is not None
    tiny = _run(_Conn(sigs[:2], rows[:2] + rows[-1:], cal), end=cal[-1])["by_horizon"]["5"]
    assert tiny["ci_method"] is None and tiny["ci90"]["avg_return_edge"] is None


def test_a_delisted_exit_is_counted_and_a_window_not_yet_closed_is_left_out() -> None:
    cal = _cal(30)
    sigs = [("AAA", cal[0]), ("AAA", cal[20])]
    rows = [
        _row("s", "AAA", cal[0], 1, -0.30, 0, 0, 0, 1),  # exited at the last close
        _row("s", "AAA", cal[20], 0, 0.0, 0, 0, 0, 0),  # window not elapsed
        _row("b", "AAA", None, 5, 0.0, 2, 2, 0),
    ]
    h = _run(_Conn(sigs, rows, cal))["by_horizon"]["5"]
    assert h["signal"]["n"] == 1 and h["n_raw"] == 1 and h["delisted_exits"] == 1


def test_the_aggregate_query_is_given_the_spliced_tickers_and_the_delisted_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(listing_lineage, "_PREDECESSOR", {"ECHO": "SATS"})
    monkeypatch.setattr(listing_lineage, "_SUCCESSOR", {"SATS": "ECHO"})
    monkeypatch.setattr(listing_lineage, "_HANDOVERS", {("SATS", "ECHO"): date(2026, 3, 16)})
    monkeypatch.setattr(signal_stats, "listing_ends", lambda conn, syms, as_of: {"WBS": date(2026, 3, 20)})
    cal = _cal(40)
    # A SATS row before the handover is ECHO's; an ECHO row before it is another company's.
    sigs = [("SATS", cal[1]), ("ECHO", cal[2]), ("ECHO", cal[15]), ("WBS", cal[3])]
    conn = _Conn(sigs, [], cal)
    out = _run(conn, symbols=["ECHO", "WBS"])
    assert out["signals"] == 3
    p = conn.params
    assert sorted(set(p["sig_sym"])) == ["ECHO", "WBS"]
    assert cal[2] not in [d for s, d in zip(p["sig_sym"], p["sig_d"]) if s == "ECHO"]
    assert p["tickers"] == ["ECHO", "SATS", "WBS"] and p["delisted"] == ["WBS"]
    assert p["lin_cut_0"] == date(2026, 3, 16)


def test_spliced_bars_sql_and_in_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(listing_lineage, "_PREDECESSOR", {"ECHO": "SATS", "IA": "ISSC"})
    monkeypatch.setattr(listing_lineage, "_SUCCESSOR", {"SATS": "ECHO", "ISSC": "IA"})
    monkeypatch.setattr(listing_lineage, "_HANDOVERS", {("SATS", "ECHO"): date(2026, 3, 16)})
    monkeypatch.setattr(listing_lineage, "handover", lambda conn, dead, alive: None)
    label, keep, params, tickers = listing_lineage.spliced_bars_sql(None, ["echo", "IA", "NVDA"])
    assert tickers == ["ECHO", "IA", "ISSC", "NVDA", "SATS"]
    assert "WHEN symbol = %(lin_dead_" in label
    # ISSC has no handover on record: its bars stay out, as stock_clause reads it.
    assert "ISSC" in params.values() and keep.startswith("NOT (")
    cut = date(2026, 3, 16)
    assert listing_lineage.in_lineage(None, "SATS", cut - timedelta(days=1))
    assert not listing_lineage.in_lineage(None, "SATS", cut)
    assert not listing_lineage.in_lineage(None, "ECHO", cut - timedelta(days=1))
    assert listing_lineage.in_lineage(None, "ECHO", cut)
    assert listing_lineage.in_lineage(None, "NVDA", cut)
    assert listing_lineage.in_lineage(None, "IA", cut) and not listing_lineage.in_lineage(None, "ISSC", cut)


def test_detail_lists_per_symbol_and_recent_signals_with_what_the_cooldown_counted() -> None:
    cal = _cal(40)
    sigs = {"AAA": [cal[0], cal[2]], "BBB": [cal[5]]}
    rows = [
        _row("s", "AAA", cal[0], 1, 0.05, 1, 1, 1),
        _row("s", "AAA", cal[2], 1, -0.01, 0, 0, 0),
        _row("s", "BBB", cal[5], 1, 0.02, 1, 1, 1),
        _row("b", "AAA", None, 20, 0.10, 11, 12, 3),
        _row("b", "BBB", None, 20, 0.00, 9, 10, 0),
    ]
    out = signal_stats.evaluate(
        _Conn([], rows, cal), sigs, sign=1, start=MON, end=MON + timedelta(days=60), horizons=[5],
        move_threshold=0.02, today=MON + timedelta(days=200), detail=True,
    )
    assert out["per_symbol"]["AAA"]["signals"] == 2 and out["per_symbol"]["AAA"]["by_horizon"]["5"]["n"] == 1
    assert [(r["symbol"], r["date"], r["counted_5"]) for r in out["recent"]] == [
        ("BBB", cal[5].isoformat(), True),
        ("AAA", cal[2].isoformat(), False),
        ("AAA", cal[0].isoformat(), True),
    ]
    assert out["recent"][1]["ret_5"] == -0.01
