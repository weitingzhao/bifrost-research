"""Frozen threshold set and the mechanical sources (design §5, §7).

The thresholds are fixed before counting starts. Changing any value means a new
``THRESHOLDS["version"]`` and the clock restarts from the change (design §7):
the version is what a scoreboard reads, so a quiet edit cannot slip through.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# Owner 2026-10-05 23:32 UTC: §7 as recommended.
THRESHOLDS: dict[str, Any] = {
    # .2 (2026-10-06): the delta rule below, added after the first night's
    # suggestions picked 35/39/27-delta puts under 30/20-delta rules.
    "version": "2026-10-06.2",
    "counts_from": "first counted suggestion issued after the ledger went live; forward samples only",
    "min_months": 6,
    "regimes": {
        "label": "SPY iv_percentile_1y at the suggestion's as_of_session (features.option_metric_iv_percentile_daily)",
        "low_below": 30,
        "high_above": 70,
        "min_settled_each": 20,
    },
    "sample": {
        "kind": "option_structure",
        "min_settled": 100,
        "one_per_symbol_per_iso_week": True,
        "max_share_per_symbol": 0.10,
        # Set once, after the baseline's first month: max(100, (z * sd / mean)^2)
        # with the measured return-on-risk sd. The rule is frozen here; the number
        # it yields is written into the next version.
        "recompute_rule": "after the baseline's first settled month: max(100, ceil((1.2816 * sd / 0.04) ** 2))",
    },
    "expectancy": {"basis": "model_stress", "slippage_scale": 1.5, "metric": "return_on_risk", "ci": "bootstrap 90% lower bound > 0"},
    "beats_baseline": {"basis": "baseline_paired", "metric": "paired return_on_risk difference", "ci": "90% lower bound > 0"},
    "tail": {"max_drawdown_vs_baseline": 1.2, "worst_trade": "within the suggestion's stated max_loss"},
    "adoption": {"actual_minus_model_max_share_of_edge": 0.5},
    "per_source": True,
    "delta_rule": "a mechanical suggestion counts only when its picked short delta is within DELTA_TOLERANCE of its rule's",
    "stand_aside": "reported only; not counted toward P&L thresholds",
}

# How each settlement basis prices the walk. ``model_stress`` is the 1.5x
# slippage of threshold 4; ``baseline_paired`` uses ``model`` pricing on SPY.
SETTLEMENT_METHOD_VERSION = "walk-1"
BASIS_SLIPPAGE: dict[str, float] = {"model": 1.0, "model_stress": 1.5, "baseline_paired": 1.0}
MAX_STALE_SESSIONS = 3

BASELINE_SYMBOL = "SPY"

# Design §5.1: every week's first session, SPY, sell the 30-delta put about 45
# DTE, take profit at 50%, close at 21 DTE. No stop (none was specified).
BASELINE: dict[str, Any] = {
    "source_ref": "spy_weekly_30d_put",
    # 2: delta guard + 16:00 snapshot bars (0.174.1); v1 picked a 35-delta put.
    "source_version": "2",
    "symbol": BASELINE_SYMBOL,
    "structure": "short_put",
    "short_delta": 0.30,
    "target_dte": 45,
    "min_dte": 30,
    "take_profit_pct": 0.5,
    "stop_loss_mult": None,
    "exit_dte": 21,
    "cadence": "weekly",
}

# Simulator configs running as paper suggestions (design §8). Default until the
# Owner names others: the simulator's own default configuration (short put,
# 20 delta, ~45 DTE, take 50%, stop at 2x credit, close at 21 DTE) on the three
# index ETFs, weekly. Change = new source_version.
SIMULATOR_LIVE: tuple[dict[str, Any], ...] = (
    {
        "source_ref": "sim_default_short_put_20d",
        "source_version": "2",
        "symbols": ("SPY", "QQQ", "IWM"),
        "structure": "short_put",
        "short_delta": 0.20,
        "target_dte": 45,
        "min_dte": 30,
        "wing_width_pct": 0.05,
        "take_profit_pct": 0.5,
        "stop_loss_mult": 2.0,
        "exit_dte": 21,
        "cadence": "weekly",
    },
)

# Pine scripts as a suggestion source (S4, Owner 2026-10-06): Pine says when,
# Bifrost says what. A buy signal sells the 20-delta put, a sell signal sells the
# 20-delta call credit spread; everything else is the live simulator config's
# (~45 DTE, take 50%, stop at 2x credit, close at 21 DTE). ``source_ref`` is the
# script id and ``source_version`` the script's library version (Owner).
#
# Forward samples only: signals before ``live_from`` are history, never issued;
# a signal from a script version other than the one pinned here is not issued
# either -- a new version is a script written after seeing the sessions its full
# rebuild re-signals, so it starts counting only when it is pinned here with a
# new ``live_from``.
#
# Issuance rules (per script; ``rule_version`` moves when any of them does):
# - one suggestion per (script, symbol, session): ``issue_key``; a session where
#   the script fired both sides on a symbol issues neither;
# - one per (script, symbol, ISO week) -- the threshold's counting unit;
# - no same-direction repeat within ``cooldown_days`` (target DTE - exit DTE:
#   the previous one would still be open);
# - a symbol stays within ``max_share_per_symbol`` of the script's suggestions
#   since ``live_from`` (its first one is always allowed);
# - at most ``daily_cap_per_side`` per script, side and session, taken in a
#   hash order of (session, script, side, symbol) -- unbiased, reproducible;
# - the universe is whatever the snapshot can pick a 20-delta for: a name with
#   no chain, or whose nearest strike misses the delta by more than
#   DELTA_TOLERANCE, is skipped (and does not use up the cap).
_SIM = SIMULATOR_LIVE[0]
PINE_LIVE: dict[str, Any] = {
    "rule_version": "1",
    "live_from": date(2026, 10, 6),
    "scripts": (
        {"script_id": "supertrend", "script_version": 1},
        {"script_id": "donchian_breakout", "script_version": 1},
    ),
    "sides": {
        "buy": {"structure": "short_put", "short_delta": 0.20},
        "sell": {"structure": "call_credit_spread", "short_delta": 0.20},
    },
    "target_dte": _SIM["target_dte"],
    "min_dte": _SIM["min_dte"],
    "wing_width_pct": _SIM["wing_width_pct"],
    "take_profit_pct": _SIM["take_profit_pct"],
    "stop_loss_mult": _SIM["stop_loss_mult"],
    "exit_dte": _SIM["exit_dte"],
    "cooldown_days": int(_SIM["target_dte"]) - int(_SIM["exit_dte"]),
    "per_symbol_per_iso_week": 1,
    "max_share_per_symbol": THRESHOLDS["sample"]["max_share_per_symbol"],
    "daily_cap_per_side": 5,
}

# A mechanical source refuses to issue when the nearest strike's delta is
# further than this from its rule's target (the suggestion would not be the rule).
DELTA_TOLERANCE = 0.05

# A missed run may still issue for a session this many sessions back — the
# mechanical sources have no discretion, so a late issue sees nothing the rule
# would not have. ``issued_at`` records when it was written.
CATCH_UP_SESSIONS = 5
# Per source and session, to keep correlated mechanical suggestions bounded.
DAILY_CAP: dict[str, int] = {"baseline": 1, "simulator": 10}

__all__ = [
    "BASELINE",
    "BASELINE_SYMBOL",
    "BASIS_SLIPPAGE",
    "CATCH_UP_SESSIONS",
    "DAILY_CAP",
    "DELTA_TOLERANCE",
    "MAX_STALE_SESSIONS",
    "PINE_LIVE",
    "SETTLEMENT_METHOD_VERSION",
    "SIMULATOR_LIVE",
    "THRESHOLDS",
]
