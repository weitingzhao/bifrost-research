"""Strategy templates for the event-driven backtest engine (Wave RS-C1).

Each template returns a list of ``LegSpec`` describing the legs to enter at
``entry_date`` and exit at ``exit_date`` — anchored around an ``event_date``
by ``entry_offset_days`` / ``exit_offset_days``.

The templates are *specs*, not orders — the query engine consumes them to
price legs against ``raw_market.option_daily`` + ``raw_market.stock_daily``.

D10 BLOCKED — historical replay only. No path here ever reaches an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable, Literal, Mapping

Side = Literal["buy", "sell"]
LegKind = Literal["option", "stock"]
OptionRight = Literal["C", "P"]


@dataclass(frozen=True)
class LegSpec:
    """A single leg of a strategy template.

    ``entry_offset_days`` / ``exit_offset_days`` are relative to the
    ``event_date`` (negative = before, 0 = on event day, positive = after).

    For options:
      - ``target_dte`` picks the expiry closest to ``entry_date + target_dte``
      - ``target_delta`` (if set) picks the closest-delta strike; otherwise
        ``target_moneyness_offset`` (relative to ATM, in pct of spot) is used
        (0.0 = ATM). ``option_right`` is required.

    For stock legs, ``option_right``, ``target_dte``, ``target_delta`` are
    ignored; ``quantity`` is share count.
    """

    kind: LegKind
    side: Side
    quantity: int = 1
    entry_offset_days: int = 0
    exit_offset_days: int = 0
    option_right: OptionRight | None = None
    target_dte: int = 30
    target_delta: float | None = None
    target_moneyness_offset: float = 0.0
    label: str = ""


TemplateFn = Callable[..., list[LegSpec]]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def long_atm_straddle(
    *,
    entry_offset_days: int = -1,
    exit_offset_days: int = 1,
    target_dte: int = 30,
    quantity: int = 1,
) -> list[LegSpec]:
    """Long ATM call + long ATM put — the canonical event volatility play."""
    return [
        LegSpec(
            kind="option",
            side="buy",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="C",
            target_dte=target_dte,
            target_moneyness_offset=0.0,
            label="ATM call",
        ),
        LegSpec(
            kind="option",
            side="buy",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="P",
            target_dte=target_dte,
            target_moneyness_offset=0.0,
            label="ATM put",
        ),
    ]


def short_atm_straddle(
    *,
    entry_offset_days: int = -1,
    exit_offset_days: int = 1,
    target_dte: int = 30,
    quantity: int = 1,
) -> list[LegSpec]:
    """Short ATM call + short ATM put — sell vol into the event."""
    return [
        LegSpec(
            kind="option",
            side="sell",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="C",
            target_dte=target_dte,
            target_moneyness_offset=0.0,
            label="short ATM call",
        ),
        LegSpec(
            kind="option",
            side="sell",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="P",
            target_dte=target_dte,
            target_moneyness_offset=0.0,
            label="short ATM put",
        ),
    ]


def long_atm_call(
    *,
    entry_offset_days: int = -1,
    exit_offset_days: int = 1,
    target_dte: int = 30,
    quantity: int = 1,
) -> list[LegSpec]:
    return [
        LegSpec(
            kind="option",
            side="buy",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="C",
            target_dte=target_dte,
            target_moneyness_offset=0.0,
            label="ATM call",
        ),
    ]


def long_atm_put(
    *,
    entry_offset_days: int = -1,
    exit_offset_days: int = 1,
    target_dte: int = 30,
    quantity: int = 1,
) -> list[LegSpec]:
    return [
        LegSpec(
            kind="option",
            side="buy",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="P",
            target_dte=target_dte,
            target_moneyness_offset=0.0,
            label="ATM put",
        ),
    ]


def short_30d_iron_condor(
    *,
    entry_offset_days: int = -1,
    exit_offset_days: int = 1,
    target_dte: int = 30,
    short_delta: float = 0.25,
    wing_width_pct: float = 0.05,
    quantity: int = 1,
) -> list[LegSpec]:
    """Short 30d iron condor — sell 25-delta strangle, buy protective wings.

    Wings are placed ``wing_width_pct`` further OTM as pct of spot (v1
    simplification — bid/ask picking is done in the query engine).
    """
    return [
        LegSpec(
            kind="option",
            side="sell",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="C",
            target_dte=target_dte,
            target_delta=short_delta,
            label=f"short {int(short_delta * 100)}-delta call",
        ),
        LegSpec(
            kind="option",
            side="buy",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="C",
            target_dte=target_dte,
            target_moneyness_offset=+wing_width_pct,
            label="long call wing",
        ),
        LegSpec(
            kind="option",
            side="sell",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="P",
            target_dte=target_dte,
            target_delta=short_delta,
            label=f"short {int(short_delta * 100)}-delta put",
        ),
        LegSpec(
            kind="option",
            side="buy",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="P",
            target_dte=target_dte,
            target_moneyness_offset=-wing_width_pct,
            label="long put wing",
        ),
    ]


def covered_call_1sd(
    *,
    entry_offset_days: int = 0,
    exit_offset_days: int = 30,
    target_dte: int = 30,
    quantity: int = 1,
    call_moneyness_offset: float = 0.05,
) -> list[LegSpec]:
    """Own stock + sell an OTM call ~1 stdev above spot (v1: fixed pct offset).

    Stock leg quantity is ``quantity * 100`` shares to match one contract.
    """
    return [
        LegSpec(
            kind="stock",
            side="buy",
            quantity=quantity * 100,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            label="long stock",
        ),
        LegSpec(
            kind="option",
            side="sell",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="C",
            target_dte=target_dte,
            target_moneyness_offset=call_moneyness_offset,
            label=f"short +{int(call_moneyness_offset * 100)}% call",
        ),
    ]


TEMPLATES: Mapping[str, TemplateFn] = {
    "long_atm_straddle": long_atm_straddle,
    "short_atm_straddle": short_atm_straddle,
    "long_atm_call": long_atm_call,
    "long_atm_put": long_atm_put,
    "short_30d_iron_condor": short_30d_iron_condor,
    "covered_call_1sd": covered_call_1sd,
}


def get_template(name: str) -> TemplateFn:
    fn = TEMPLATES.get(name)
    if fn is None:
        raise ValueError(
            f"unknown strategy template {name!r}; available: {sorted(TEMPLATES)}"
        )
    return fn


def build_legs(name: str, **kwargs: object) -> list[LegSpec]:
    """Build legs via template + kwargs. Extra kwargs are dropped silently."""
    fn = get_template(name)
    sig_kwargs = {k: v for k, v in kwargs.items() if k in fn.__code__.co_varnames}
    return fn(**sig_kwargs)


def resolve_leg_window(leg: LegSpec, event_date: date) -> tuple[date, date]:
    """Absolute (entry_date, exit_date) for ``leg`` anchored on ``event_date``."""
    entry = event_date + timedelta(days=int(leg.entry_offset_days))
    exit_ = event_date + timedelta(days=int(leg.exit_offset_days))
    if exit_ < entry:
        entry, exit_ = exit_, entry
    return entry, exit_


def leg_signs(leg: LegSpec) -> int:
    """+1 for buy legs, -1 for sell legs (used to sign P&L contributions)."""
    return +1 if leg.side == "buy" else -1


def iter_legs(legs: Iterable[LegSpec]) -> list[LegSpec]:
    """Materialize a legs iterable and validate all specs."""
    out = list(legs)
    for leg in out:
        if leg.kind == "option" and leg.option_right not in ("C", "P"):
            raise ValueError(f"option leg missing option_right: {leg}")
        if leg.quantity <= 0:
            raise ValueError(f"leg.quantity must be positive: {leg}")
    return out


__all__ = [
    "LegSpec",
    "Side",
    "LegKind",
    "OptionRight",
    "TEMPLATES",
    "long_atm_straddle",
    "short_atm_straddle",
    "long_atm_call",
    "long_atm_put",
    "short_30d_iron_condor",
    "covered_call_1sd",
    "get_template",
    "build_legs",
    "resolve_leg_window",
    "leg_signs",
    "iter_legs",
]
