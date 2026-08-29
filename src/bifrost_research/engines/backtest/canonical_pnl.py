"""Canonical structure PnL pricing library — Wave Canonical-PnL Foundation.

Five delta-parameterized structures for Watchlist / signal trust replay.
Historical mark-to-market only (D10 BLOCKED — no live execution).

Structures:
  short_strangle | put_credit_spread | long_straddle | covered_call | short_put
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Mapping, Sequence

StructureName = Literal[
    "short_strangle",
    "put_credit_spread",
    "long_straddle",
    "covered_call",
    "short_put",
]

DataQuality = Literal["ok", "insufficient_chain", "iv_interpolated"]

STRUCTURES: tuple[StructureName, ...] = (
    "short_strangle",
    "put_credit_spread",
    "long_straddle",
    "covered_call",
    "short_put",
)

_DEFAULT_PARAMS: dict[StructureName, dict[str, Any]] = {
    "short_strangle": {
        "short_call_delta": 0.15,
        "short_put_delta": -0.15,
        "dte": 45,
    },
    "put_credit_spread": {
        "short_delta": -0.30,
        "width": 5.0,
        "dte": 45,
    },
    "long_straddle": {"dte": 30},
    "covered_call": {
        "short_call_delta": 0.30,
        "dte": 30,
        "own_stock": 100,
    },
    "short_put": {"short_delta": -0.30, "dte": 30},
}


@dataclass(frozen=True)
class StructureParams:
    structure: StructureName
    params: Mapping[str, Any]

    def canonical_dict(self) -> dict[str, Any]:
        base = dict(_DEFAULT_PARAMS[self.structure])
        base.update({k: v for k, v in self.params.items() if v is not None})
        return {"structure": self.structure, **base}

    def params_hash(self) -> str:
        blob = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LegMark:
    """One option or stock leg at a valuation date."""

    kind: Literal["option", "stock"]
    side: Literal["buy", "sell"]
    quantity: int
    right: Literal["C", "P"] | None = None
    strike: float | None = None
    expiry: date | None = None
    mid: float | None = None
    label: str = ""


@dataclass(frozen=True)
class StructureMark:
    structure: StructureName
    params_hash: str
    structure_params: dict[str, Any]
    entry_date: date
    as_of_date: date
    entry_spot: float | None
    entry_atm_iv: float | None
    entry_mid: float | None
    as_of_spot: float | None
    as_of_atm_iv: float | None
    mtm_value: float | None
    pnl_since_entry: float | None
    dte_remaining: int | None
    expired: bool
    final_pnl: float | None
    data_quality: DataQuality
    legs: tuple[LegMark, ...] = ()

    def to_row(self, symbol: str) -> dict[str, Any]:
        return {
            "as_of_date": self.as_of_date,
            "entry_date": self.entry_date,
            "symbol": symbol.upper(),
            "structure": self.structure,
            "params_hash": self.params_hash,
            "structure_params": self.structure_params,
            "entry_spot": self.entry_spot,
            "entry_atm_iv": self.entry_atm_iv,
            "entry_mid": self.entry_mid,
            "as_of_spot": self.as_of_spot,
            "as_of_atm_iv": self.as_of_atm_iv,
            "mtm_value": self.mtm_value,
            "pnl_since_entry": self.pnl_since_entry,
            "dte_remaining": self.dte_remaining,
            "expired": self.expired,
            "final_pnl": self.final_pnl,
            "data_quality": self.data_quality,
        }


def default_params(structure: StructureName) -> StructureParams:
    return StructureParams(structure=structure, params=dict(_DEFAULT_PARAMS[structure]))


# ---------------------------------------------------------------------------
# Black–Scholes helpers (European, continuous rates ≈ 0 for short-dated equity)
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    *,
    right: Literal["C", "P"],
    rate: float = 0.0,
) -> float:
    """Black–Scholes mid price. ``iv`` is decimal (0.25 = 25%)."""
    if spot <= 0 or strike <= 0 or iv <= 0:
        return max(0.0, (spot - strike) if right == "C" else (strike - spot))
    if t_years <= 1e-8:
        return max(0.0, (spot - strike) if right == "C" else (strike - spot))
    vol = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / vol
    d2 = d1 - vol
    if right == "C":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t_years) * _norm_cdf(d2)
    return strike * math.exp(-rate * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    *,
    right: Literal["C", "P"],
    rate: float = 0.0,
) -> float:
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 1e-8:
        if right == "C":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    vol = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / vol
    if right == "C":
        return float(_norm_cdf(d1))
    return float(_norm_cdf(d1) - 1.0)


def strike_for_delta(
    spot: float,
    t_years: float,
    iv: float,
    target_delta: float,
    *,
    right: Literal["C", "P"],
) -> float:
    """Invert BS delta → strike via binary search on moneyness."""
    if spot <= 0 or iv <= 0 or t_years <= 1e-8:
        return spot
    lo, hi = spot * 0.3, spot * 2.5
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        d = bs_delta(spot, mid, t_years, iv, right=right)
        if right == "C":
            # higher strike → lower call delta
            if d > target_delta:
                lo = mid
            else:
                hi = mid
        else:
            # put delta is negative; more negative → lower strike
            if d < target_delta:
                hi = mid
            else:
                lo = mid
    return 0.5 * (lo + hi)


def _signed_premium(side: Literal["buy", "sell"], mid: float, qty: int, multiplier: int = 100) -> float:
    """Cash flow at open: sell → +credit, buy → -debit (per structure net)."""
    cash = mid * qty * multiplier
    return cash if side == "sell" else -cash


def _mtm_value(side: Literal["buy", "sell"], mid: float, qty: int, multiplier: int = 100) -> float:
    """Current mark value of a held position (long = +mid, short = -mid)."""
    cash = mid * qty * multiplier
    return cash if side == "buy" else -cash


def build_entry_legs(
    structure: StructureName,
    *,
    spot: float,
    atm_iv: float,
    entry_date: date,
    params: Mapping[str, Any] | None = None,
) -> tuple[list[LegMark], StructureParams, DataQuality]:
    """Construct theoretical legs at entry from spot + ATM IV (no chain required)."""
    sp = StructureParams(structure=structure, params=params or {})
    p = sp.canonical_dict()
    dte = int(p.get("dte") or 30)
    t = max(dte, 1) / 365.0
    iv = float(atm_iv)
    quality: DataQuality = "iv_interpolated"
    legs: list[LegMark] = []

    if structure == "short_strangle":
        c_delta = float(p["short_call_delta"])
        p_delta = float(p["short_put_delta"])
        c_k = strike_for_delta(spot, t, iv, c_delta, right="C")
        p_k = strike_for_delta(spot, t, iv, p_delta, right="P")
        legs = [
            LegMark("option", "sell", 1, "C", c_k, None, bs_price(spot, c_k, t, iv, right="C"), "short call"),
            LegMark("option", "sell", 1, "P", p_k, None, bs_price(spot, p_k, t, iv, right="P"), "short put"),
        ]
    elif structure == "put_credit_spread":
        short_d = float(p["short_delta"])
        width = float(p["width"])
        short_k = strike_for_delta(spot, t, iv, short_d, right="P")
        long_k = short_k - width
        legs = [
            LegMark("option", "sell", 1, "P", short_k, None, bs_price(spot, short_k, t, iv, right="P"), "short put"),
            LegMark("option", "buy", 1, "P", long_k, None, bs_price(spot, long_k, t, iv, right="P"), "long put"),
        ]
    elif structure == "long_straddle":
        legs = [
            LegMark("option", "buy", 1, "C", spot, None, bs_price(spot, spot, t, iv, right="C"), "ATM call"),
            LegMark("option", "buy", 1, "P", spot, None, bs_price(spot, spot, t, iv, right="P"), "ATM put"),
        ]
    elif structure == "covered_call":
        c_delta = float(p["short_call_delta"])
        own = int(p.get("own_stock") or 100)
        c_k = strike_for_delta(spot, t, iv, c_delta, right="C")
        legs = [
            LegMark("stock", "buy", own, None, None, None, spot, "long stock"),
            LegMark("option", "sell", 1, "C", c_k, None, bs_price(spot, c_k, t, iv, right="C"), "short call"),
        ]
    elif structure == "short_put":
        short_d = float(p["short_delta"])
        short_k = strike_for_delta(spot, t, iv, short_d, right="P")
        legs = [
            LegMark("option", "sell", 1, "P", short_k, None, bs_price(spot, short_k, t, iv, right="P"), "short put"),
        ]
    else:
        raise ValueError(f"unknown structure: {structure}")

    # stamp synthetic expiry
    from datetime import timedelta

    expiry = entry_date + timedelta(days=dte)
    legs = [
        LegMark(
            kind=lg.kind,
            side=lg.side,
            quantity=lg.quantity,
            right=lg.right,
            strike=lg.strike,
            expiry=expiry if lg.kind == "option" else None,
            mid=lg.mid,
            label=lg.label,
        )
        for lg in legs
    ]
    return legs, sp, quality


def net_entry_credit(legs: Sequence[LegMark]) -> float:
    total = 0.0
    for lg in legs:
        mid = float(lg.mid or 0.0)
        if lg.kind == "stock":
            # stock: buy = cash out
            total += -mid * lg.quantity if lg.side == "buy" else mid * lg.quantity
        else:
            total += _signed_premium(lg.side, mid, lg.quantity)
    return total


def mark_structure(
    legs: Sequence[LegMark],
    *,
    structure: StructureName,
    params: StructureParams,
    entry_date: date,
    as_of_date: date,
    entry_spot: float,
    entry_atm_iv: float,
    entry_mid: float,
    as_of_spot: float,
    as_of_atm_iv: float,
    data_quality: DataQuality = "iv_interpolated",
) -> StructureMark:
    """Reprice held legs at as_of with BS using as_of spot/IV; compute PnL."""
    from datetime import timedelta

    marked: list[LegMark] = []
    mtm = 0.0
    expired = False
    dte_remaining: int | None = None

    for lg in legs:
        if lg.kind == "stock":
            mid = as_of_spot
            marked.append(
                LegMark(lg.kind, lg.side, lg.quantity, None, None, None, mid, lg.label)
            )
            mtm += _mtm_value(lg.side, mid, lg.quantity, multiplier=1)
            continue

        expiry = lg.expiry or (entry_date + timedelta(days=int(params.canonical_dict().get("dte") or 30)))
        dte = (expiry - as_of_date).days
        dte_remaining = dte if dte_remaining is None else min(dte_remaining, dte)
        strike = float(lg.strike or as_of_spot)
        right = lg.right or "C"
        if dte <= 0:
            expired = True
            mid = max(0.0, (as_of_spot - strike) if right == "C" else (strike - as_of_spot))
        else:
            mid = bs_price(as_of_spot, strike, dte / 365.0, as_of_atm_iv, right=right)
        marked.append(
            LegMark(lg.kind, lg.side, lg.quantity, right, strike, expiry, mid, lg.label)
        )
        mtm += _mtm_value(lg.side, mid, lg.quantity)

    # For credit structures entry_mid is typically positive (net credit received).
    # PnL = entry_cashflow + current_mtm  where entry_cashflow = entry_mid (already signed).
    # Convention: entry_mid from net_entry_credit (sell positive); mtm is mark of position.
    # At entry, mtm ≈ -entry_mid for pure option shorts → pnl ≈ 0.
    pnl = entry_mid + mtm
    final = pnl if expired else None

    return StructureMark(
        structure=structure,
        params_hash=params.params_hash(),
        structure_params=params.canonical_dict(),
        entry_date=entry_date,
        as_of_date=as_of_date,
        entry_spot=entry_spot,
        entry_atm_iv=entry_atm_iv,
        entry_mid=entry_mid,
        as_of_spot=as_of_spot,
        as_of_atm_iv=as_of_atm_iv,
        mtm_value=mtm,
        pnl_since_entry=pnl,
        dte_remaining=dte_remaining,
        expired=expired,
        final_pnl=final,
        data_quality=data_quality,
        legs=tuple(marked),
    )


def simulate_trajectory(
    structure: StructureName,
    *,
    entry_date: date,
    as_of_dates: Sequence[date],
    spots: Mapping[date, float],
    atm_ivs: Mapping[date, float],
    params: Mapping[str, Any] | None = None,
) -> list[StructureMark]:
    """Build entry legs once, mark through ``as_of_dates``."""
    entry_spot = spots.get(entry_date)
    entry_iv = atm_ivs.get(entry_date)
    if entry_spot is None or entry_iv is None or entry_spot <= 0 or entry_iv <= 0:
        # insufficient — emit null-pnl rows
        sp = StructureParams(structure=structure, params=params or {})
        return [
            StructureMark(
                structure=structure,
                params_hash=sp.params_hash(),
                structure_params=sp.canonical_dict(),
                entry_date=entry_date,
                as_of_date=d,
                entry_spot=entry_spot,
                entry_atm_iv=entry_iv,
                entry_mid=None,
                as_of_spot=spots.get(d),
                as_of_atm_iv=atm_ivs.get(d),
                mtm_value=None,
                pnl_since_entry=None,
                dte_remaining=None,
                expired=False,
                final_pnl=None,
                data_quality="insufficient_chain",
            )
            for d in as_of_dates
        ]

    legs, sp, quality = build_entry_legs(
        structure, spot=entry_spot, atm_iv=entry_iv, entry_date=entry_date, params=params
    )
    entry_mid = net_entry_credit(legs)
    out: list[StructureMark] = []
    for d in as_of_dates:
        if d < entry_date:
            continue
        spot = spots.get(d)
        iv = atm_ivs.get(d)
        if spot is None or iv is None or spot <= 0 or iv <= 0:
            out.append(
                StructureMark(
                    structure=structure,
                    params_hash=sp.params_hash(),
                    structure_params=sp.canonical_dict(),
                    entry_date=entry_date,
                    as_of_date=d,
                    entry_spot=entry_spot,
                    entry_atm_iv=entry_iv,
                    entry_mid=entry_mid,
                    as_of_spot=spot,
                    as_of_atm_iv=iv,
                    mtm_value=None,
                    pnl_since_entry=None,
                    dte_remaining=None,
                    expired=False,
                    final_pnl=None,
                    data_quality="insufficient_chain",
                )
            )
            continue
        out.append(
            mark_structure(
                legs,
                structure=structure,
                params=sp,
                entry_date=entry_date,
                as_of_date=d,
                entry_spot=entry_spot,
                entry_atm_iv=entry_iv,
                entry_mid=entry_mid,
                as_of_spot=spot,
                as_of_atm_iv=iv,
                data_quality=quality,
            )
        )
    return out


__all__ = [
    "STRUCTURES",
    "StructureName",
    "StructureParams",
    "StructureMark",
    "LegMark",
    "DataQuality",
    "default_params",
    "bs_price",
    "bs_delta",
    "strike_for_delta",
    "build_entry_legs",
    "net_entry_credit",
    "mark_structure",
    "simulate_trajectory",
]
