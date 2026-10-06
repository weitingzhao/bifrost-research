"""option_surface_iv_daily replaces a (symbol, trade_date), it does not merge into it (TD-112)."""

from __future__ import annotations

from datetime import date

import pytest

from bifrost_research.engines.volatility import surface

TD = date(2026, 9, 3)


class _Cur:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, sql, params=None):
        self.log.append(("execute", " ".join(sql.split()), params))

    def executemany(self, sql, rows):
        self.log.append(("executemany", " ".join(sql.split()), len(rows)))


class _Conn:
    def __init__(self) -> None:
        self.log: list = []

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        self.log.append(("commit", "", None))

    def rollback(self):
        self.log.append(("rollback", "", None))


def _fit(expiries: list[str]) -> dict:
    return {
        "smiles": [{"expiry": e, "fit_model": "svi", "rmse": 0.01, "n_points": 9} for e in expiries],
        "surface_points": [],
        "vol_cone": {},
    }


def test_a_rewalk_deletes_the_day_before_writing_its_expiries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surface, "fetch_iv_points_for_date", lambda c, s, d: (100.0, {"x": [1]}))
    monkeypatch.setattr(surface, "fetch_atm_iv_history", lambda c, s, d: [])
    monkeypatch.setattr(surface, "fit_iv_surface", lambda by, spot, history_ivs: _fit(["2026-09-19"]))
    conn = _Conn()
    out = surface.compute_iv_surface_for_symbol(conn, symbol="spy", trade_date=TD)
    assert out["rows_written"] == 1
    kinds = [(k, (sql.split() or [""])[0]) for k, sql, _ in conn.log]
    assert kinds == [("execute", "DELETE"), ("executemany", "INSERT"), ("commit", "")]
    _, sql, params = conn.log[0]
    assert "features.option_surface_iv_daily" in sql and "expiry" not in sql
    assert params == ("SPY", TD)


def test_a_day_without_iv_points_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surface, "fetch_iv_points_for_date", lambda c, s, d: (None, {}))
    conn = _Conn()
    out = surface.compute_iv_surface_for_symbol(conn, symbol="SPY", trade_date=TD)
    assert out["ok"] is False
    assert conn.log == []
