"""Pure-compute tests for GEX engine (no DB)."""

from __future__ import annotations

from bifrost_research.engines.gex.exposure import (
    ContractGreeks,
    approx_bs_gamma,
    compute_gex_distribution,
    compute_gex_levels,
    gex_notional,
    strike_gex_from_contracts,
)


def test_approx_bs_gamma_positive() -> None:
    g = approx_bs_gamma(100.0, 100.0, iv=0.25, t_years=30 / 365)
    assert g > 0


def test_gex_sign_convention() -> None:
    spot = 100.0
    gamma = 0.05
    call = gex_notional(gamma, 1000, spot, sign=1.0)
    put = gex_notional(gamma, 1000, spot, sign=-1.0)
    assert call > 0
    assert put < 0
    assert abs(call + put) < 1e-9


def test_strike_distribution_and_walls() -> None:
    contracts = [
        ContractGreeks(strike=95.0, option_right="P", open_interest=5000, gamma=0.02),
        ContractGreeks(strike=100.0, option_right="C", open_interest=2000, gamma=0.04),
        ContractGreeks(strike=100.0, option_right="P", open_interest=2000, gamma=0.04),
        ContractGreeks(strike=105.0, option_right="C", open_interest=8000, gamma=0.03),
    ]
    dist, levels = compute_gex_distribution(contracts, spot=100.0)
    assert len(dist) == 3
    assert levels["major_call_wall"] == 105.0
    assert levels["major_put_wall"] == 95.0
    assert levels["zero_gamma"] is not None
    assert levels["total_net_gex"] == sum(r["net_gex"] for r in dist)


def test_volume_source_flag() -> None:
    contracts = [
        ContractGreeks(
            strike=100.0, option_right="C", open_interest=100, volume=50, gamma=0.02
        ),
    ]
    rows = strike_gex_from_contracts(contracts, 100.0)
    assert rows[0]["gex_source"].endswith("+volume")
    assert rows[0]["volume_net_gex"] != 0.0


def test_levels_empty() -> None:
    levels = compute_gex_levels([], 100.0)
    assert levels["total_net_gex"] == 0.0
    assert levels["zero_gamma"] is None


# ─── 2026-09-26: intraday spot stands on the latest close ───

from datetime import date  # noqa: E402
from typing import Self  # noqa: E402

from bifrost_research.engines.gex.exposure import fetch_spot, fetch_spot_reading  # noqa: E402


class _SpotCursor:
    """Answers the spot queries from a table of (sql fragment → row)."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.last: object = None
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        self.calls.append((sql, params))
        self.last = None
        for frag, row in self.answers.items():
            if frag in sql:
                self.last = row
                return

    def fetchone(self) -> object:
        return self.last

    def fetchall(self) -> list:
        return self.last if isinstance(self.last, list) else []


class _SpotConn:
    def __init__(self, answers: dict[str, object]) -> None:
        self.cur = _SpotCursor(answers)

    def cursor(self) -> _SpotCursor:
        return self.cur


def test_intraday_spot_falls_back_to_the_prior_close_only_when_asked() -> None:
    # During the session neither exact-date table holds today: stock_daily and
    # stock_snapshot are written after the close.
    conn = _SpotConn({"bar_date < %s": (189.67, date(2026, 9, 25))})
    assert fetch_spot(conn, "PLTR", date(2026, 9, 28)) is None
    reading = fetch_spot_reading(conn, "PLTR", date(2026, 9, 28), prior_close_days=7)
    assert reading == (189.67, "prior_close", date(2026, 9, 25))
    sql, params = conn.cur.calls[-1]
    assert "ORDER BY bar_date DESC" in sql
    assert params == ("PLTR", date(2026, 9, 28), date(2026, 9, 21))


def test_the_days_own_close_wins_over_the_prior_close() -> None:
    conn = _SpotConn({"bar_date = %s": (190.5,), "bar_date < %s": (189.67, date(2026, 9, 25))})
    assert fetch_spot_reading(conn, "PLTR", date(2026, 9, 28), prior_close_days=7) == (
        190.5,
        "close",
        date(2026, 9, 28),
    )


def test_index_spot_still_falls_back_to_the_max_oi_strike() -> None:
    conn = _SpotConn({"SUM(open_interest)": (6600.0,)})
    reading = fetch_spot_reading(conn, "SPX", date(2026, 9, 28), prior_close_days=7)
    assert reading == (6600.0, "oi_max_strike", date(2026, 9, 28))


def test_index_spot_is_priced_by_parity_before_the_max_oi_strike() -> None:
    # SPX 2026-09-25: F = K + C − P on the nearest expiry at the five strikes
    # where C and P are closest; the max-OI strike read 7000 (SPY closed 771.35).
    fwds = [(7745.1,), (7745.53,), (7745.4,), (7744.9,), (7745.5,)]
    conn = _SpotConn({"c.strike + c.px - p.px": fwds, "SUM(open_interest)": (7000.0,)})
    reading = fetch_spot_reading(conn, "SPX", date(2026, 9, 25), prior_close_days=7)
    assert reading == (7745.4, "parity", date(2026, 9, 25))
    _, params = next(c for c in conn.cur.calls if "c.strike + c.px - p.px" in c[0])
    assert params[0] == "SPX" and params[3] == date(2026, 9, 25)


def test_session_parity_prices_a_stock_before_the_prior_close() -> None:
    # PLTR 2026-09-25 at the 13:00 intraday chain: parity 191.97 against the hour's
    # bar at 191.86, where the prior close stood at 192.59.
    fwds = [(191.9,), (192.1,), (191.97,), (191.8,), (192.0,)]
    conn = _SpotConn({"c.strike + c.px - p.px": fwds, "bar_date < %s": (192.59, date(2026, 9, 24))})
    assert fetch_spot_reading(conn, "PLTR", date(2026, 9, 25), prior_close_days=7) == (
        192.59,
        "prior_close",
        date(2026, 9, 24),
    )
    reading = fetch_spot_reading(conn, "PLTR", date(2026, 9, 25), prior_close_days=7, session_parity=True)
    assert reading == (191.97, "parity", date(2026, 9, 25))


def test_session_parity_falls_back_to_the_prior_close_without_a_chain() -> None:
    conn = _SpotConn({"bar_date < %s": (192.59, date(2026, 9, 24))})
    reading = fetch_spot_reading(conn, "PLTR", date(2026, 9, 25), prior_close_days=7, session_parity=True)
    assert reading == (192.59, "prior_close", date(2026, 9, 24))
