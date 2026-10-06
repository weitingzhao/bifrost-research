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
from typing import Callable, Iterable, Literal, Mapping, Sequence

Side = Literal["buy", "sell"]
LegKind = Literal["option", "stock"]
OptionRight = Literal["C", "P"]


@dataclass(frozen=True)
class LegSpec:
    """A single leg of a strategy template.

    ``entry_offset_days`` / ``exit_offset_days`` count **trading sessions**
    from the event's session (the first session on or after ``event_date``):
    -1 is the last session before the event, 0 the event's own session, +1
    the next. Until 0.169.0 they were calendar days, so a Friday event with
    exit +1 landed on Saturday, fell back to Friday, and held for zero days.

    For options:
      - ``target_dte`` picks the expiry closest to ``entry_date + target_dte``
        (calendar days — expiries are dates, not sessions)
      - ``target_delta`` (if set) picks the strike whose Black–Scholes delta,
        from the IV implied by that day's bar, is closest to it (absolute
        value, so 0.25 means a 25-delta put as well as call); otherwise
        ``target_moneyness_offset`` (relative to ATM, in pct of spot) is used
        (0.0 = ATM). ``option_right`` is required.
      - ``anchor_leg`` (if set) is the index of an earlier option leg: this
        leg takes that leg's expiry and strikes ``anchor_offset_pct`` of spot
        away from that leg's strike (an iron condor's wings). The moneyness
        and delta targets are then ignored.
      - A leg whose exit falls on or after its expiry is settled at intrinsic
        value against the underlying's close on expiry day.

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
    anchor_leg: int | None = None
    anchor_offset_pct: float = 0.0


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
    """Short 30d iron condor — sell a ``short_delta`` strangle, buy wings.

    Each wing sits ``wing_width_pct`` of spot beyond its short strike, on the
    short leg's expiry. Until 0.169.0 the short legs' ``target_delta`` was
    ignored (both sold ATM) and the wings sat at spot ± 5%, so this template
    priced an ATM iron butterfly under a condor's name.
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
            label=f"short {int(round(short_delta * 100))}-delta call",
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
            label=f"short {int(round(short_delta * 100))}-delta put",
        ),
        LegSpec(
            kind="option",
            side="buy",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="C",
            target_dte=target_dte,
            anchor_leg=0,
            anchor_offset_pct=+abs(wing_width_pct),
            label="long call wing",
        ),
        LegSpec(
            kind="option",
            side="buy",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            option_right="P",
            target_dte=target_dte,
            anchor_leg=1,
            anchor_offset_pct=-abs(wing_width_pct),
            label="long put wing",
        ),
    ]


def short_strangle_30d(
    *,
    entry_offset_days: int = -1,
    exit_offset_days: int = 1,
    target_dte: int = 30,
    short_delta: float = 0.16,
    quantity: int = 1,
) -> list[LegSpec]:
    """Short ``short_delta`` call + put, ~30 DTE — the premium-selling thesis.

    The auto-validate hook has named this template since B4, but it was never
    registered, so an option-leg validation would have raised on lookup.
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
            label=f"short {int(round(short_delta * 100))}-delta call",
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
            label=f"short {int(round(short_delta * 100))}-delta put",
        ),
    ]


def covered_call_1sd(
    *,
    entry_offset_days: int = 0,
    exit_offset_days: int = 21,
    target_dte: int = 30,
    quantity: int = 1,
    call_moneyness_offset: float = 0.05,
) -> list[LegSpec]:
    """Own stock + sell an OTM call ~1 stdev above spot (v1: fixed pct offset).

    Stock leg quantity is ``quantity * 100`` shares to match one contract.
    The default exit is 21 sessions (~30 calendar days, the call's expiry):
    offsets count sessions since 0.169.0, and the call is settled at
    intrinsic if the exit reaches its expiry.
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


def long_stock_event(
    *,
    entry_offset_days: int = -1,
    exit_offset_days: int = 2,
    quantity: int = 100,
) -> list[LegSpec]:
    """Hold the underlying across the event — no option leg.

    Every other template prices against ``raw_market.option_daily``, which today
    holds a few weeks of history, so a multi-year event study returns nothing at
    all. ``raw_market.stock_daily`` covers 15 months and ~15k symbols, and the
    first question about an event thesis is whether the underlying moves at all
    — before any question about structure or IV.
    """
    return [
        LegSpec(
            kind="stock",
            side="buy",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            label="long stock",
        ),
    ]


def short_stock_event(
    *,
    entry_offset_days: int = -1,
    exit_offset_days: int = 2,
    quantity: int = 100,
) -> list[LegSpec]:
    """Short the underlying across the event — the mirror of ``long_stock_event``.

    Kept as its own template rather than a flag so a fade thesis reads as a fade
    in the run record, instead of a long with a negated sign.
    """
    return [
        LegSpec(
            kind="stock",
            side="sell",
            quantity=quantity,
            entry_offset_days=entry_offset_days,
            exit_offset_days=exit_offset_days,
            label="short stock",
        ),
    ]


TEMPLATES: Mapping[str, TemplateFn] = {
    "long_stock_event": long_stock_event,
    "short_stock_event": short_stock_event,
    "long_atm_straddle": long_atm_straddle,
    "short_atm_straddle": short_atm_straddle,
    "long_atm_call": long_atm_call,
    "long_atm_put": long_atm_put,
    "short_30d_iron_condor": short_30d_iron_condor,
    "short_strangle_30d": short_strangle_30d,
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


def resolve_trading_window(
    leg: LegSpec, event_date: date, sessions: Sequence[date], *, after_event: bool = False
) -> tuple[date, date] | None:
    """(entry_date, exit_date) for ``leg``, counting sessions from the event.

    ``sessions`` are the underlying's trading days, ascending, around the
    event. Offset 0 is the first session on or after ``event_date`` — or, with
    ``after_event`` (a signal computed from that session's close, 0.175.0),
    the first session strictly after it. Returns None when the window runs off
    either end of ``sessions`` — an event too recent to have an exit yet must
    be skipped, not priced on the last bar.
    """
    days = sorted(set(sessions))
    anchor = next((i for i, d in enumerate(days) if (d > event_date if after_event else d >= event_date)), None)
    if anchor is None:
        return None
    lo, hi = sorted((int(leg.entry_offset_days), int(leg.exit_offset_days)))
    i_entry, i_exit = anchor + lo, anchor + hi
    if i_entry < 0 or i_exit >= len(days):
        return None
    return days[i_entry], days[i_exit]


def resolve_leg_window(leg: LegSpec, event_date: date) -> tuple[date, date]:
    """Calendar-day (entry_date, exit_date) — the pre-0.169.0 reading.

    Kept for callers that want a rough window to fetch sessions over; pricing
    uses ``resolve_trading_window``.
    """
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
    for i, leg in enumerate(out):
        if leg.anchor_leg is None:
            continue
        if not 0 <= leg.anchor_leg < i or out[leg.anchor_leg].kind != "option":
            raise ValueError(f"anchor_leg must name an earlier option leg: {leg}")
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
    "short_strangle_30d",
    "covered_call_1sd",
    "get_template",
    "build_legs",
    "resolve_leg_window",
    "resolve_trading_window",
    "leg_signs",
    "iter_legs",
]
