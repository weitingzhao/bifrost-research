"""Unit tests for canonical structure PnL pricing (no DB / IB)."""

from __future__ import annotations

from datetime import date, timedelta

from bifrost_research.engines.backtest.canonical_pnl import (
    STRUCTURES,
    bs_delta,
    bs_price,
    build_entry_legs,
    default_params,
    mark_structure,
    net_entry_credit,
    simulate_trajectory,
    strike_for_delta,
)


def test_bs_call_put_parity_rough():
    spot, k, t, iv = 100.0, 100.0, 30 / 365.0, 0.25
    c = bs_price(spot, k, t, iv, right="C")
    p = bs_price(spot, k, t, iv, right="P")
    assert c > 0 and p > 0
    # put-call parity at r=0: C - P ≈ S - K
    assert abs((c - p) - (spot - k)) < 0.5


def test_strike_for_call_delta_otm():
    spot, t, iv = 100.0, 45 / 365.0, 0.30
    k = strike_for_delta(spot, t, iv, 0.15, right="C")
    d = bs_delta(spot, k, t, iv, right="C")
    assert k > spot
    assert abs(d - 0.15) < 0.03


def test_short_strangle_entry_credit_positive():
    entry = date(2026, 1, 15)
    legs, sp, q = build_entry_legs(
        "short_strangle", spot=100.0, atm_iv=0.25, entry_date=entry
    )
    credit = net_entry_credit(legs)
    assert credit > 0
    assert len(legs) == 2
    assert q in ("ok", "iv_interpolated")
    assert sp.params_hash()


def test_long_straddle_entry_debit():
    entry = date(2026, 1, 15)
    legs, _, _ = build_entry_legs(
        "long_straddle", spot=100.0, atm_iv=0.25, entry_date=entry
    )
    assert net_entry_credit(legs) < 0


def test_mark_pnl_near_zero_at_entry():
    entry = date(2026, 1, 15)
    legs, sp, q = build_entry_legs(
        "short_put", spot=100.0, atm_iv=0.22, entry_date=entry
    )
    entry_mid = net_entry_credit(legs)
    m = mark_structure(
        legs,
        structure="short_put",
        params=sp,
        entry_date=entry,
        as_of_date=entry,
        entry_spot=100.0,
        entry_atm_iv=0.22,
        entry_mid=entry_mid,
        as_of_spot=100.0,
        as_of_atm_iv=0.22,
        data_quality=q,
    )
    assert m.pnl_since_entry is not None
    assert abs(m.pnl_since_entry) < 5.0  # pennies of BS / rounding


def test_simulate_trajectory_spot_up_hurts_short_put():
    entry = date(2026, 1, 2)
    days = [entry + timedelta(days=i) for i in range(0, 21, 5)]
    spots = {d: 100.0 + i * 2 for i, d in enumerate(days)}
    ivs = {d: 0.25 for d in days}
    marks = simulate_trajectory(
        "short_put",
        entry_date=entry,
        as_of_dates=days,
        spots=spots,
        atm_ivs=ivs,
    )
    assert len(marks) == len(days)
    # spot up → short put should improve (or stay non-worse than deep ITM start)
    assert marks[-1].pnl_since_entry is not None
    assert marks[0].pnl_since_entry is not None


def test_insufficient_when_missing_spot():
    entry = date(2026, 1, 2)
    marks = simulate_trajectory(
        "long_straddle",
        entry_date=entry,
        as_of_dates=[entry],
        spots={},
        atm_ivs={},
    )
    assert marks[0].data_quality == "insufficient_chain"
    assert marks[0].pnl_since_entry is None


def test_locf_fill_iv_carries_within_gap():
    from bifrost_research.engines.canonical_pnl.compute import locf_fill_iv

    d0 = date(2026, 1, 2)
    d1 = date(2026, 1, 5)  # weekend gap
    d2 = date(2026, 1, 20)  # beyond 14d
    filled = locf_fill_iv({d0: 0.22}, [d0, d1, d2], max_gap_days=14)
    assert filled[d0] == 0.22
    assert filled[d1] == 0.22
    assert d2 not in filled


def test_all_structures_build():
    entry = date(2026, 3, 1)
    for s in STRUCTURES:
        legs, sp, _ = build_entry_legs(s, spot=150.0, atm_iv=0.28, entry_date=entry)
        assert legs
        assert default_params(s).structure == s
        assert sp.params_hash()


# ─── 0.111.0: canonical PnL prices on IV30, the reading VRP and the percentile use ───

from datetime import date as _date, timedelta as _td  # noqa: E402

from bifrost_research.engines.canonical_pnl.compute import fetch_atm_iv_series  # noqa: E402


class _IvCur:
    def __init__(self, atm: list, vrp: list) -> None:
        self.atm, self.vrp, self._rows = atm, vrp, []

    def execute(self, sql: str, params=None) -> None:
        self._rows = self.atm if "option_metric_atm_iv_daily" in sql else self.vrp

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None


class _IvConn:
    def __init__(self, atm: list, vrp: list) -> None:
        self.cur = _IvCur(atm, vrp)

    def cursor(self):
        return self.cur

    def rollback(self) -> None:
        raise AssertionError("the IV30 query must not fail into the fallback")


def test_iv_series_is_iv30_not_the_expiry_nearest_30_days() -> None:
    d1, d2 = _date(2026, 6, 25), _date(2026, 6, 26)
    atm = [
        (d1, d1 + _td(days=21), 0.50),
        (d1, d1 + _td(days=49), 0.57),
        (d2, d2 + _td(days=30), 0.52),
    ]
    out = fetch_atm_iv_series(_IvConn(atm, []), "PLTR", d1, d2)
    assert abs(out[d1] - (0.50 + 0.07 * 9 / 28)) < 1e-9
    assert out[d2] == 0.52


def test_iv_series_falls_back_to_vrp_when_the_atm_table_is_empty() -> None:
    d1 = _date(2026, 6, 25)
    out = fetch_atm_iv_series(_IvConn([], [(d1, 0.51)]), "PLTR", d1, d1)
    assert out == {d1: 0.51}


# ─── 0.117.0: a fixed weekly entry phase, nightly refresh of open entries, on-demand trajectories ───

import bifrost_research.engines.canonical_pnl.compute as cp  # noqa: E402


def _sessions(start: _date, end: _date) -> list[_date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += _td(days=1)
    return out


def test_entries_do_not_move_when_the_window_slides() -> None:
    """PLTR on 2026-09-24 vs 09-25 shared none of its 26 entries under the old stride."""
    days = _sessions(_date(2026, 3, 1), _date(2026, 9, 30))
    a_end, b_end = _date(2026, 9, 24), _date(2026, 9, 25)
    a = cp.phase_entries([d for d in days if d <= a_end], start=a_end - _td(days=183))
    b = cp.phase_entries([d for d in days if d <= b_end], start=b_end - _td(days=183))
    overlap = set(a) & set(b)
    assert len(overlap) >= len(a) - 1
    assert all(d.weekday() == 0 for d in a)  # one per ISO week, on its first session


def test_a_week_that_starts_before_the_window_is_left_out() -> None:
    days = _sessions(_date(2026, 3, 23), _date(2026, 4, 10))
    entries = cp.phase_entries(days, start=_date(2026, 3, 25))  # a Wednesday
    assert entries[0] == _date(2026, 3, 30)


def test_a_holiday_monday_moves_the_entry_to_tuesday() -> None:
    days = [d for d in _sessions(_date(2026, 8, 31), _date(2026, 9, 11)) if d != _date(2026, 9, 7)]
    assert cp.phase_entries(days, start=_date(2026, 8, 31)) == [_date(2026, 8, 31), _date(2026, 9, 8)]


def test_nightly_refresh_recomputes_only_open_entries_and_prunes_off_phase(monkeypatch) -> None:
    end = _date(2026, 9, 25)
    days = _sessions(end - _td(days=190), end)
    monkeypatch.setattr(cp, "fetch_spot_series", lambda c, s, a, b: {d: 100.0 for d in days if a <= d <= b})
    monkeypatch.setattr(cp, "fetch_atm_iv_series", lambda c, s, a, b: {d: 0.5 for d in days if a <= d <= b})
    seen: dict[str, list] = {}
    monkeypatch.setattr(cp, "prune_symbol", lambda c, s, *, start, entries: seen.setdefault("prune", list(entries)) and 0)
    monkeypatch.setattr(cp, "run_symbol_window", lambda c, **kw: seen.setdefault("todo", list(kw["entry_dates"])) and {"rows_written": 0})

    class _C:
        commits = 0

        def commit(self) -> None:
            _C.commits += 1

    cp.run_cohort(_C(), symbols=["PLTR"], as_of=end, coverage=False, refresh_days=cp.MARK_HORIZON_DAYS)
    assert len(seen["prune"]) >= 25  # the whole window's phase is kept
    assert seen["todo"] and all(e >= end - _td(days=60) for e in seen["todo"])
    assert len(seen["todo"]) < len(seen["prune"])
    assert _C.commits == 1


def test_a_weekend_hypothesis_is_simulated_from_the_next_session(monkeypatch) -> None:
    days = _sessions(_date(2026, 9, 1), _date(2026, 9, 25))
    spots = {d: 100.0 + i for i, d in enumerate(days)}
    monkeypatch.setattr(cp, "fetch_spot_series", lambda c, s, a, b: {d: v for d, v in spots.items() if a <= d <= b})
    monkeypatch.setattr(cp, "fetch_atm_iv_series", lambda c, s, a, b: {d: 0.5 for d in days if a <= d <= b})
    out = cp.simulate_entry(None, symbol="pltr", entry_date=_date(2026, 9, 12), structure="short_strangle", as_of_end=_date(2026, 9, 25))
    assert out["entry_date"] == _date(2026, 9, 14)
    assert out["rows"] and {r["entry_date"] for r in out["rows"]} == {_date(2026, 9, 14)}
    assert cp.mark_row_json(out["rows"][0])["entry_date"] == "2026-09-14"
