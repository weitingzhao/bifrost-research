"""Seller structures the simulator opens.

A leg is (right, side, how-to-pick): by target delta, or anchored to an earlier
leg — same expiry, ``offset_pct`` of spot beyond that leg's strike.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SimLeg:
    right: Literal["C", "P"]
    side: Literal["buy", "sell"]
    by_delta: bool = True
    anchor: int | None = None
    offset_sign: int = 0  # +1 above the anchor strike, -1 below
    label: str = ""


STRUCTURES: dict[str, tuple[SimLeg, ...]] = {
    "short_put": (SimLeg("P", "sell", label="short put"),),
    "put_credit_spread": (
        SimLeg("P", "sell", label="short put"),
        SimLeg("P", "buy", by_delta=False, anchor=0, offset_sign=-1, label="long put"),
    ),
    "short_strangle": (
        SimLeg("C", "sell", label="short call"),
        SimLeg("P", "sell", label="short put"),
    ),
    "iron_condor": (
        SimLeg("C", "sell", label="short call"),
        SimLeg("P", "sell", label="short put"),
        SimLeg("C", "buy", by_delta=False, anchor=0, offset_sign=+1, label="long call"),
        SimLeg("P", "buy", by_delta=False, anchor=1, offset_sign=-1, label="long put"),
    ),
}

__all__ = ["STRUCTURES", "SimLeg"]
