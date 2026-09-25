"""Unit tests for Historical IV Brent solver (no DB required for core)."""

from __future__ import annotations

import pytest

from bifrost_research.engines.backtest.canonical_pnl import bs_price
from bifrost_research.engines.volatility.iv_solver import solve_iv


KNOWN_CASES = [
    # spot, strike, tte, right, known_iv
    (100.0, 100.0, 30 / 365.0, "C", 0.25),
    (100.0, 100.0, 45 / 365.0, "P", 0.30),
    (150.0, 145.0, 21 / 365.0, "C", 0.40),
    (80.0, 85.0, 60 / 365.0, "P", 0.22),
    (250.0, 260.0, 14 / 365.0, "C", 0.55),
]


@pytest.mark.parametrize("spot,strike,tte,right,known_iv", KNOWN_CASES)
def test_solve_iv_round_trip(spot, strike, tte, right, known_iv):
    mid = bs_price(spot, strike, tte, known_iv, right=right)
    iv, status = solve_iv(spot, strike, tte, mid, right)
    assert status == "ok"
    assert iv is not None
    assert abs(iv - known_iv) < 1e-3


def test_insufficient_when_mid_below_intrinsic():
    # Call intrinsic = 10, mid too low
    iv, status = solve_iv(110.0, 100.0, 30 / 365.0, 5.0, "C")
    assert status == "insufficient_inputs"
    assert iv is None


def test_insufficient_zero_spot():
    iv, status = solve_iv(0.0, 100.0, 0.1, 5.0, "C")
    assert status == "insufficient_inputs"


def test_polygon_style_atm_backcheck_tolerance():
    """Synthetic stand-in for Polygon ATM back-check: 20 nearby IVs."""
    rel_errs: list[float] = []
    for i in range(20):
        spot = 100.0 + i
        known = 0.18 + i * 0.01
        tte = (20 + i) / 365.0
        mid = bs_price(spot, spot, tte, known, right="C")
        iv, status = solve_iv(spot, spot, tte, mid, "C")
        assert status == "ok" and iv is not None
        rel_errs.append(abs(iv - known) / known)
    median = sorted(rel_errs)[len(rel_errs) // 2]
    assert median < 0.03
    assert max(rel_errs) < 0.05


# ─── vendor IV wins over Brent for the same contract-day ───

from datetime import date  # noqa: E402

from bifrost_research.engines.volatility.iv_solver import solve_symbol_window  # noqa: E402


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn
        self._rows: list = []

    def execute(self, sql: str, params=None) -> None:
        if "solver_status = 'vendor_snapshot'" in sql:
            self._rows = self.conn.vendor
        elif "raw_market.option_daily" in sql:
            self._rows = self.conn.daily
        else:
            self._rows = []

    def executemany(self, sql: str, seq) -> None:
        self.conn.upserts.extend(list(seq))

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None


class _Conn:
    def __init__(self, vendor, daily) -> None:
        self.vendor, self.daily, self.upserts = vendor, daily, []

    def cursor(self):
        return _Cur(self)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_brent_keeps_existing_vendor_rows():
    td, exp = date(2026, 8, 12), date(2026, 9, 18)
    spot = 171.04
    mid = bs_price(spot, 170.0, (exp - td).days / 365.0, 0.47, right="C")
    daily = [
        ("O:PLTR260918C00170000", "PLTR", td, exp, 170.0, "C", None, mid, mid, mid, spot),
        ("O:PLTR260918C00175000", "PLTR", td, exp, 175.0, "C", None, mid, mid, mid * 0.8, spot),
    ]
    conn = _Conn(vendor=[("O:PLTR260918C00170000", td)], daily=daily)
    out = solve_symbol_window(conn, "PLTR", td, td)
    assert out["vendor_kept"] == 1
    assert [r[1] for r in conn.upserts] == ["O:PLTR260918C00175000"]



def test_vendor_projection_requires_the_row_to_be_fetched_near_its_session():
    from bifrost_research.engines.volatility.iv_solver import project_vendor_snapshot_window

    seen: list[str] = []

    class _C(_Cur):
        def execute(self, sql, params=None):
            seen.append(sql)
            self._rows = []

    class _K(_Conn):
        def cursor(self):
            return _C(self)

    project_vendor_snapshot_window(_K(vendor=[], daily=[]), "PLTR", date(2026, 8, 5), date(2026, 8, 7), dry_run=True)
    assert any("fetched_at" in q and "<= 3" in q for q in seen)


# ─── 0.118.0: a partial EOD snapshot is not projected ───

from bifrost_research.engines.volatility import iv_solver as ivs  # noqa: E402


class _StatCur:
    def __init__(self, conn) -> None:
        self.conn, self._rows, self.rowcount = conn, [], 0

    def execute(self, sql, params=None) -> None:
        self.conn.sql.append(sql)
        if "percentile_cont" in sql:
            self._rows = self.conn.stats
        elif "v_option_snapshot_with_stock" in sql:
            self._rows = self.conn.snap
        elif sql.lstrip().startswith("DELETE"):
            self.rowcount = 7
        else:
            self._rows = []

    def executemany(self, sql, seq) -> None:
        self.conn.upserts.extend(list(seq))

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None


class _StatConn:
    def __init__(self, stats, snap=()) -> None:
        self.stats, self.snap, self.sql, self.upserts, self.commits = list(stats), list(snap), [], [], 0

    def cursor(self):
        return _StatCur(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


D = [date(2026, 9, d) for d in (15, 16, 17, 18, 21, 22, 23)]


def test_partial_snapshot_needs_both_fewer_contracts_and_higher_iv():
    """CDW 2026-09-22: 27 contracts at 1.21 against ~51 at ~0.46."""
    stats = [(d, 51, 0.46) for d in D]
    stats[5] = (D[5], 27, 1.21)
    assert ivs.degraded_sessions(_StatConn(stats), "CDW", D[4], D[6]) == {D[5]}
    thin_only = [(d, 51, 0.46) for d in D]
    thin_only[5] = (D[5], 27, 0.47)  # fewer quotes, same vol: a thin day
    assert ivs.degraded_sessions(_StatConn(thin_only), "CDW", D[4], D[6]) == set()
    move_only = [(d, 51, 0.46) for d in D]
    move_only[5] = (D[5], 50, 1.21)  # full chain, vol up: a real move
    assert ivs.degraded_sessions(_StatConn(move_only), "CDW", D[4], D[6]) == set()


def test_one_prior_session_is_enough_to_judge():
    """PRU entered the snapshot on 09-21 and broke on 09-22 (38 → 21, 0.26 → 0.83)."""
    stats = [(D[4], 38, 0.262), (D[5], 21, 0.825), (D[6], 41, 0.263)]
    assert ivs.degraded_sessions(_StatConn(stats), "PRU", D[4], D[6]) == {D[5]}


def test_projection_skips_and_clears_a_partial_session():
    stats = [(d, 51, 0.46) for d in D]
    stats[5] = (D[5], 27, 1.21)
    exp = date(2026, 10, 16)
    snap = [
        ("O:CDW261016C00145000", "CDW", D[5], exp, 145.0, "C", 1.21, 147.43, 0.5, 0.01),
        ("O:CDW261016C00145000", "CDW", D[6], exp, 145.0, "C", 0.45, 146.16, 0.5, 0.01),
    ]
    conn = _StatConn(stats, snap)
    out = ivs.project_vendor_snapshot_window(conn, "CDW", D[4], D[6])
    assert out["degraded_sessions"] == ["2026-09-22"]
    assert out["degraded_rows_dropped"] == 7
    assert [r[2] for r in conn.upserts] == [D[6]]
    assert any(s.lstrip().startswith("DELETE") and "vendor_snapshot" in s for s in conn.sql)
