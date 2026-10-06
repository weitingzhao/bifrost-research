"""One Black–Scholes, one Treasury reader, and the rate is never implicit (TD-110)."""

from __future__ import annotations

import inspect
import logging
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from bifrost_research import pricing
from bifrost_research.pricing import RateCurve, bs_price, load_rate_curve, risk_free_rate, solve_iv

_SRC = Path(__file__).resolve().parents[1] / "src" / "bifrost_research"
_PRICING = _SRC / "pricing"

# ── ratchet: no second copy ───────────────────────────────────────────────

_COPY = re.compile(
    r"def _?norm_(?:cdf|pdf)\b|def _?bs_(?:price|delta|gamma|vanna|charm)\b|def _?solve_iv\b|math\.erf\b"
    r"|FROM\s+raw_market\.treasury_yield",
    re.I,
)


def test_black_scholes_and_the_treasury_read_live_only_in_pricing() -> None:
    hits = []
    for path in sorted(_SRC.rglob("*.py")):
        if _PRICING in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        for m in _COPY.finditer(text):
            hits.append(f"{path.relative_to(_SRC)}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}")
    assert not hits, "a Black–Scholes or Treasury copy outside bifrost_research.pricing:\n" + "\n".join(hits)


@pytest.mark.parametrize("name", ["bs_price", "bs_delta", "bs_gamma", "bs_vanna", "bs_charm", "solve_iv"])
def test_rate_is_a_required_keyword(name: str) -> None:
    param = inspect.signature(getattr(pricing, name)).parameters["rate"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, f"{name}: a default rate would let r = 0 come back silently"


# ── the curve ─────────────────────────────────────────────────────────────

D = date(2026, 9, 25)


class _Cur:
    def __init__(self, conn) -> None:
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, sql, params=None):
        self.conn.params = params
        if self.conn.error:
            raise self.conn.error

    def fetchall(self):
        return self.conn.rows


class _Conn:
    def __init__(self, rows=(), error: Exception | None = None) -> None:
        self.rows, self.error, self.params, self.rolled_back = list(rows), error, None, False

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        self.rolled_back = True


def test_percent_and_decimal_feeds_read_the_same_and_stale_or_absurd_values_do_not() -> None:
    curve = load_rate_curve(_Conn([(D - timedelta(days=3), 4.06), (D, 0.0404), (D + timedelta(days=1), 31.0)]), D, D)
    assert curve.on_or_before(D) == pytest.approx(0.0404)
    assert curve.on_or_before(D - timedelta(days=1)) == pytest.approx(0.0406)
    assert curve.on_or_before(D + timedelta(days=1)) == pytest.approx(0.0404)  # 31% is not a T-bill
    assert curve.on_or_before(D + timedelta(days=30)) == 0.0  # older than MAX_STALENESS_DAYS
    assert curve.on_or_before(D - timedelta(days=30)) == 0.0


def test_the_window_reaches_back_for_a_holiday_week() -> None:
    conn = _Conn([])
    load_rate_curve(conn, D, D + timedelta(days=5))
    assert conn.params == (D - timedelta(days=pricing.MAX_STALENESS_DAYS), D + timedelta(days=5))


def test_an_unreadable_table_prices_at_zero_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    conn = _Conn(error=RuntimeError("permission denied"))
    with caplog.at_level(logging.WARNING):
        assert risk_free_rate(conn, D) == 0.0
    assert conn.rolled_back and "r=0" in caplog.text


def test_the_single_date_reader_memoises() -> None:
    cache: dict[date, float] = {}
    assert risk_free_rate(_Conn([(D, 4.0)]), D, cache) == pytest.approx(0.04)
    assert risk_free_rate(_Conn(error=RuntimeError("not read again")), D, cache) == pytest.approx(0.04)


# ── the stored IV features use it ─────────────────────────────────────────


def test_brent_iv_features_solve_at_the_sessions_treasury_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost_research.engines.volatility import iv_solver

    td, exp, spot, rate = date(2026, 8, 12), date(2026, 9, 18), 171.04, 0.043
    tte = (exp - td).days / 365.0
    mid = bs_price(spot, 175.0, tte, 0.47, right="P", rate=rate)
    rows = [("O:PLTR260918P00175000", "PLTR", td, exp, 175.0, "P", None, mid, mid, mid, spot)]

    class _C(_Conn):
        def cursor(self):
            conn = self

            class C(_Cur):
                def execute(self, sql, params=None):
                    conn.rows = [] if "vendor_snapshot" in sql else rows

                def executemany(self, sql, seq):
                    conn.upserts = list(seq)

            return C(conn)

        def commit(self):
            return None

    monkeypatch.setattr(iv_solver, "load_rate_curve", lambda _c, _s, _e: RateCurve({td: rate}))
    conn = _C()
    iv_solver.solve_symbol_window(conn, "PLTR", td, td)
    stored_iv = conn.upserts[0][iv_solver._COLS.index("iv")]
    assert stored_iv == pytest.approx(0.47, abs=1e-3)
    at_zero, _ = solve_iv(spot, 175.0, tte, mid, "P", rate=0.0)
    assert abs(at_zero - stored_iv) > 0.005  # r = 0 would have stored a different IV
