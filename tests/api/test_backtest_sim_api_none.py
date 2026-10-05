"""SimBody: None turns a management rule off, max_stale_sessions included."""

from __future__ import annotations

from bifrost_research.api.backtest_sim import SimBody


def test_every_management_rule_accepts_none() -> None:
    body = SimBody(symbols=["NVDA"], profit_take_pct=None, stop_loss_mult=None, dte_exit=None, max_stale_sessions=None)
    assert body.max_stale_sessions is None
