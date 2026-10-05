"""What each of Research's evaluations is, under one vocabulary (W2, 0.171.0).

Five separate things were all called "backtest" (review 2026-10-04 §2.1), and
read side by side they answer different questions on different prices. The
Owner's call on 2026-10-05: rename and restate first, keep every table.

Three classes:

- ``backtest``           a position priced on real prints, entry to exit. The
                         simulator manages it session by session; the event
                         backtest takes one print at entry and one at exit.
- ``model_reference``    a structure priced by Black–Scholes on one ATM IV, no
                         skew. A reference for what the shape would have paid,
                         not a price anyone traded at.
- ``signal_evaluation``  whether a signal, a proposal or a forecast was right
                         about the stock afterwards. No position, no option price.

Every evaluation's API response carries its entry as ``evaluation`` so a reader
never takes one class for another. The tables keep their names
(``features.stock_backtest_settlement`` stays) until the Owner decides on them.
"""

from __future__ import annotations

from typing import Any, Literal

EvaluationClass = Literal["backtest", "model_reference", "signal_evaluation"]

EVALUATIONS: dict[str, dict[str, Any]] = {
    "option_simulator": {
        "class": "backtest",
        "title": "Option position simulator",
        "answers": "What a short-premium structure earned, managed session by session.",
        "price_basis": "each session's vwap (close when none) plus tiered slippage; expiry at intrinsic",
        "entry": "a schedule, or an event with an offset in sessions",
        "code": "engines/backtest/sim",
        "store": "research.backtest_run (engine='sim'), research.backtest_trade, research.backtest_equity",
        "route": "POST /research/backtest/sim",
    },
    "suggestion_settlement": {
        "class": "backtest",
        "title": "Suggestion settlement",
        "answers": "What a suggestion's own legs earned under its own rules, from the session after it was issued.",
        "price_basis": "the simulator's walk: next session's vwap plus tiered slippage (x1.5 for model_stress); expiry at intrinsic",
        "entry": "each settleable suggestion; baseline_paired opens SPY with the same structure, delta and DTE",
        "code": "engines/suggestion, engines/backtest/sim/walk.py",
        "store": "research.suggestion, research.suggestion_settlement (append-only)",
        "route": "none yet (scoreboard is S6)",
    },
    "event_backtest": {
        "class": "backtest",
        "title": "Event backtest (one print in, one print out)",
        "answers": "What a template earned from a fixed session before an event to a fixed session after.",
        "price_basis": "the contract's close on the entry and exit sessions; no management in between",
        "entry": "an event with offsets in sessions",
        "code": "engines/backtest/event_query.py",
        "store": "research.backtest_run (engine='event')",
        "route": "POST /research/backtest/event-query",
        "notes": "for a managed position use the simulator with entry_event",
    },
    "canonical_pnl": {
        "class": "model_reference",
        "title": "Canonical P&L (model price)",
        "answers": "What a standard structure would have paid on the model, week by week.",
        "price_basis": "Black–Scholes on one ATM IV, no skew; every row is iv_interpolated",
        "entry": "one per ISO week",
        "code": "engines/canonical_pnl, engines/backtest/canonical_pnl.py",
        "store": "features.stock_signal_canonical_pnl_daily",
        "route": "GET /research/canonical-pnl/*",
    },
    "lens_hit_rate": {
        "class": "signal_evaluation",
        "title": "Lens hit rate",
        "answers": "Whether the stock moved the way a lens trigger said, 5 and 20 sessions on.",
        "price_basis": "stock close to close; a delisted name settles at its last close",
        "entry": "each lens trigger",
        "code": "engines/signal_hit",
        "store": "features.stock_signal_lens_hit_daily",
        "route": "GET /research/signal-decay/*",
    },
    "indicator_signal": {
        "class": "signal_evaluation",
        "title": "Indicator signal win rate",
        "answers": "Whether the stock moved the way a MACD / RSI / Bollinger / EMA crossing said, N sessions on, next to every session.",
        "price_basis": "adjusted stock close to close",
        "entry": "each crossing session's close",
        "code": "engines/indicators",
        "store": "none (computed on request)",
        "route": "GET /research/indicators/signal-stats",
    },
    "pine_signal": {
        "class": "signal_evaluation",
        "title": "Pine script signal win rate",
        "answers": "Whether the stock moved the way a Pine library script's buy or sell said, N sessions on, next to every session of the same names.",
        "price_basis": "adjusted stock close to close",
        "entry": "each session the script's plot fired",
        "code": "engines/pine, pine-runner/",
        "store": "features.stock_signal_pine_daily",
        "route": "GET /research/pine/signal-stats",
    },
    "candidate_outcome": {
        "class": "signal_evaluation",
        "title": "Candidate and hypothesis settlement",
        "answers": "Whether a name the Loop proposed beat SPY over 1, 5 and 20 sessions.",
        "price_basis": "stock close to close against SPY over the same sessions",
        "entry": "each proposed candidate",
        "code": "engines/candidate_outcome, copilot/agents/hypothesis_resolution.py",
        "store": "research.candidate_outcome, research.hypothesis",
        "route": "GET /research/candidate-outcome/*",
    },
    "forecast_settlement": {
        "class": "signal_evaluation",
        "title": "Forecast settlement",
        "answers": "Whether a session forecast's path and close came true.",
        "price_basis": "the session's stock prints against the forecast's levels",
        "entry": "each forecast session",
        "code": "engines/backtest/settlement.py",
        "store": "features.stock_backtest_settlement (name kept)",
        "route": "GET /research/backtest/settlement, GET /research/forecast/settlement",
    },
}


def evaluation(key: str) -> dict[str, Any]:
    """The catalog entry stamped on a response: its key, class, title and price basis."""
    entry = EVALUATIONS[key]
    return {
        "key": key,
        "class": entry["class"],
        "title": entry["title"],
        "price_basis": entry["price_basis"],
    }


__all__ = ["EVALUATIONS", "EvaluationClass", "evaluation"]
