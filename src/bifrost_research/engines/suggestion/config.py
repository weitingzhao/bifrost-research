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
    # .3 (Owner 2026-10-06): 10 settled per IV regime, not 20. At one baseline
    # suggestion a week the two-year replay had 15 low-IV and 14 high-IV weeks,
    # so 20 each would take 2.5-3 years against a six-month floor.
    "version": "2026-10-06.3",
    "counts_from": "first counted suggestion issued after the ledger went live; forward samples only",
    "min_months": 6,
    "regimes": {
        "label": "SPY iv_percentile_1y at the suggestion's as_of_session (features.option_metric_iv_percentile_daily)",
        "low_below": 30,
        "high_above": 70,
        "min_settled_each": 10,
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
# walk-2 (2026-10-06): entry may wait up to LIQUIDITY["max_entry_delay"]
# sessions for every leg to trade (walk-1 voided on the first missing bar).
SETTLEMENT_METHOD_VERSION = "walk-2"
BASIS_SLIPPAGE: dict[str, float] = {"model": 1.0, "model_stress": 1.5, "baseline_paired": 1.0, "symbol_paired": 1.0}
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
#
# rule_version 2 (Owner 2026-10-06, after the 2024-11..2026-10 replay,
# REPORT-pine-s4-replay-2026-10-06.md): sell -> call credit spread is no longer
# issued (-6% on risk in every IV regime and year), and the source is paused
# while the buy side is iterated offline against held-out sessions -- its prior
# was ~0 and the timing added nothing over entering a few days later. Unpausing
# is a new rule_version with a new live_from.
_SIM = SIMULATOR_LIVE[0]
PINE_LIVE: dict[str, Any] = {
    "rule_version": "2",
    "paused": True,
    "live_from": date(2026, 10, 6),
    "scripts": (
        {"script_id": "supertrend", "script_version": 1},
        {"script_id": "donchian_breakout", "script_version": 1},
    ),
    "sides": {
        "buy": {"structure": "short_put", "short_delta": 0.20},
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

# The ``symbol_paired`` basis (S4 control, Owner 2026-10-06, option A): for a
# suggestion timed by a signal, the same structure under the same rule on the
# same name, entered on a session in the ``window_days`` calendar days after the
# signal on which that source did not fire (picked by hash of the suggestion id,
# so it is reproducible). After only: a control before the signal rides the move
# that made it (replay 2024-11..2026-10: +4% on risk vs ~0% for the signal). The difference to the suggestion's own ``model``
# settlement is what the timing added; ``baseline_paired`` (SPY) also carries
# the single name's own premium. Settled once the window has passed.
# Off until the PROD CHECK is widened (schema/migrate_symbol_paired.py, Owner
# approval pending): a symbol_paired row against the old CHECK would fail the
# whole settlement transaction. Turn on with ("pine",) once applied.
SYMBOL_PAIRED: dict[str, Any] = {"sources": (), "window_days": 7}

# Every mechanical source (S4 liquidity, Owner 2026-10-06 option c):
# - issue: each leg must have traded at least ``min_leg_volume`` contracts on
#   the as-of session. Measured on single-name option_daily, 30-60 DTE,
#   2026-03..04: a contract that traded 1 printed again the next session 49% of
#   the time, 10-24: 80%, 25-99: 90%, 100+: 95%. (The 16:00 snapshot's
#   day_volume is not used to measure this: it reads ~97% "printed" in every
#   bucket, as if the last session's volume carries over.)
# - settle: entry is the first session after the as-of session on which every
#   leg trades, at most ``max_entry_delay`` sessions late; past that, void.
#
# The issue gate does not apply to ``exempt_sources`` (0.185.0): the replay of
# baseline and simulator over 2024-11..2026-10 had it refuse 20 of 101 baseline
# weeks and 73 of 303 simulator picks (SPY/QQQ/IWM contracts option_daily shows
# at 2-23 contracts on the day), while without it those sources voided none --
# walk-2's entry delay covers the ETFs -- and returns were unchanged.
LIQUIDITY: dict[str, Any] = {
    "min_leg_volume": 25,
    "max_entry_delay": 3,
    "exempt_sources": ("baseline", "simulator"),
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
    "LIQUIDITY",
    "MAX_STALE_SESSIONS",
    "PINE_LIVE",
    "SETTLEMENT_METHOD_VERSION",
    "SIMULATOR_LIVE",
    "SYMBOL_PAIRED",
    "THRESHOLDS",
]
