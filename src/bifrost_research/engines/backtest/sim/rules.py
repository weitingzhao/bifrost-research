"""Simulator configuration, fill model and margin model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PriceField = Literal["vwap", "close"]


@dataclass(frozen=True)
class SimConfig:
    """Everything a run depends on; stored verbatim as ``backtest_run.params``.

    Management rules, checked each session in this order after expiry:
      - ``profit_take_pct``: close when the open P&L reaches this share of the
        entry credit (0.5 = take half). None disables.
      - ``stop_loss_mult``: close when the loss reaches this multiple of the
        credit (2.0 = lose twice what was taken in). None disables.
      - ``dte_exit``: close when calendar days to expiry fall to this. None
        holds to expiry.
      - ``max_stale_sessions``: close when any leg has gone this many sessions
        without a print — the mark is no longer a price. None disables (the
        leg carries its last mark until another rule or expiry closes it).

    Entry: every ``entry_every_sessions`` by default. With ``entry_event`` (an
    ``EventDef`` dict such as ``{"kind": "earnings"}``) a position opens
    ``entry_offset_sessions`` from each event instead — the event backtest's
    trigger with the simulator managing the position (W2, 0.171.0). For an
    indicator or Pine signal, offset 0 is the session after the signal (0.175.0).

    ``delta_tolerance``: an entry whose nearest short strike is further than
    this from ``short_delta`` (absolute delta) is skipped as
    ``delta_off_target``. 0.05 is the suggestion ledger's DELTA_TOLERANCE.
    None accepts whatever strike is nearest.
    """

    structure: str = "short_put"
    target_dte: int = 45
    min_dte: int = 7
    short_delta: float = 0.20
    wing_width_pct: float = 0.05
    quantity: int = 1
    entry_every_sessions: int = 5
    entry_event: dict[str, Any] | None = None
    entry_offset_sessions: int = -1
    max_open_per_symbol: int = 3
    profit_take_pct: float | None = 0.5
    stop_loss_mult: float | None = 2.0
    dte_exit: int | None = 21
    max_stale_sessions: int | None = 3
    delta_tolerance: float | None = 0.05
    price_field: PriceField = "vwap"
    slippage_scale: float = 1.0
    commission_per_contract: float = 0.65
    multiplier: int = 100
    capital: float = 100_000.0
    use_treasury_rate: bool = True
    bootstrap_seed: int = 7
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Per-contract, per-side slippage in option price units, by option price. There
# is no bid/ask in option_daily; these stand in for half a typical spread and are
# meant to be recalibrated against IB live quotes. Every trade carries
# ``fill_basis`` so a reader knows the price was modelled.
_SLIP_TIERS: tuple[tuple[float, float], ...] = (
    (0.50, 0.03),
    (2.00, 0.05),
    (5.00, 0.08),
    (10.00, 0.12),
)
_SLIP_PCT_ABOVE = 0.015


def slippage(price: float, scale: float = 1.0) -> float:
    p = max(0.0, float(price))
    for ceiling, slip in _SLIP_TIERS:
        if p < ceiling:
            return slip * scale
    return p * _SLIP_PCT_ABOVE * scale


def fill_price(mark: float, side: Literal["buy", "sell"], scale: float = 1.0) -> float:
    """Pay up on a buy, give up on a sell; never below zero."""
    slip = slippage(mark, scale)
    px = mark + slip if side == "buy" else mark - slip
    return max(0.0, px)


def fill_basis(cfg: SimConfig) -> str:
    return f"{cfg.price_field}+slip_tier" + (f"x{cfg.slippage_scale:g}" if cfg.slippage_scale != 1.0 else "")


def naked_short_requirement(spot: float, strike: float, premium: float, right: str) -> float:
    """Reg-T naked short option requirement per share (CBOE rule of thumb)."""
    otm = max(0.0, strike - spot) if right == "C" else max(0.0, spot - strike)
    return max(0.20 * spot - otm + premium, 0.10 * strike + premium)


__all__ = [
    "PriceField",
    "SimConfig",
    "fill_basis",
    "fill_price",
    "naked_short_requirement",
    "slippage",
]
