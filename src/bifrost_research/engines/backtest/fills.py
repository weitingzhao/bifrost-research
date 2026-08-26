"""Realistic fill model — Wave RS-C2.

Layers a mid ± slippage price on top of the RS-C1 close-price default so the
event-driven backtest engine can be run with (bid, ask, close, commission,
multiplier) parameters when option NBBO data becomes available.

Backward compatible: when bid/ask are missing or zero, ``compute_fill_price``
degrades back to ``close`` (matching RS-C1 behavior). Existing settlement.py
callers do not need to pass a ``FillConfig`` — they are untouched.

D10 BLOCKED — no execution path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExerciseStyle = Literal["american_no_early", "european"]


@dataclass(frozen=True)
class FillConfig:
    """Realistic-fills configuration.

    Attributes:
        slippage_pct_of_spread: fraction of the (ask - bid) spread paid as
            slippage. Default 0.2 (i.e. 20% of half-spread paid on each side).
        commission_per_contract: IB retail default in USD per option contract
            (single side).
        multiplier: option contract multiplier (100 = US equity option).
        exercise_style: reserved for future assignment logic; v1 treats
            American options as European for pricing / no early exercise.
    """

    slippage_pct_of_spread: float = 0.2
    commission_per_contract: float = 0.65
    multiplier: int = 100
    exercise_style: ExerciseStyle = "american_no_early"


DEFAULT_FILL_CONFIG = FillConfig()


def compute_fill_price(
    side: Literal["buy", "sell"],
    bid: float,
    ask: float,
    close: float,
    config: FillConfig | None = None,
) -> float:
    """Return the executed fill price for one contract at one side of a trade.

    Rules:
      - If both ``bid`` and ``ask`` are positive → mid + slippage (buy) or
        mid - slippage (sell) where ``slippage = config.slippage_pct_of_spread
        * (ask - bid)``.
      - If ``bid`` / ``ask`` are missing or non-positive → fall back to
        ``close`` with **zero slippage** (matches RS-C1 default).
      - Never returns negative.
    """
    cfg = config or DEFAULT_FILL_CONFIG
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread = ask - bid
        slippage = max(0.0, cfg.slippage_pct_of_spread) * spread
        price = mid + slippage if side == "buy" else mid - slippage
    else:
        price = float(close or 0.0)

    if price < 0:  # never allow a negative fill
        price = 0.0
    return float(price)


def apply_commission(
    contract_count: int,
    config: FillConfig | None = None,
    *,
    sides: int = 2,
) -> float:
    """Total commission for ``contract_count`` contracts over ``sides`` fills.

    Default ``sides=2`` matches the entry + exit round-trip.
    """
    cfg = config or DEFAULT_FILL_CONFIG
    return float(cfg.commission_per_contract) * max(0, int(contract_count)) * max(1, int(sides))


__all__ = [
    "FillConfig",
    "DEFAULT_FILL_CONFIG",
    "ExerciseStyle",
    "compute_fill_price",
    "apply_commission",
]
