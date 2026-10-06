"""Analytical Black-Scholes Vanna and Charm — Wave RS-B-OpEx1.

Formulas from Hull / Wilmott (r = risk-free, q = dividend yield; v1 uses r=q=0).
Implemented once in ``bifrost_research.pricing`` (TD-110):

  d1 = (ln(S/K) + (r - q + 0.5*σ²)*T) / (σ*sqrt(T))
  d2 = d1 - σ*sqrt(T)

  Vanna  = ∂²V / (∂S ∂σ)
         = -exp(-q*T) * φ(d1) * d2 / σ            (same for call and put)

  Charm(call) = -q*exp(-q*T)*N(d1)
                - exp(-q*T)*φ(d1) * (2*(r-q)*T - d2*σ*sqrt(T)) / (2*T*σ*sqrt(T))

  Charm(put)  =  q*exp(-q*T)*N(-d1)
                - exp(-q*T)*φ(d1) * (2*(r-q)*T - d2*σ*sqrt(T)) / (2*T*σ*sqrt(T))

Vanna sign is identical for calls and puts. Dealer aggregation uses the
same customer-long-calls / customer-short-puts convention as ``engines.gex``:
positive dealer exposure to spot moves when net_vanna > 0.

D10 BLOCKED — read-only analytics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bifrost_research.engines.gex.exposure import ContractGreeks, MULTIPLIER
from bifrost_research.pricing import bs_charm, bs_vanna


#: r = q = 0 (v1, unchanged by TD-110): dealer vanna/charm exposure is an
#: aggregate shape, and every stored option_metric_vanna_charm_daily row used it.
VANNA_CHARM_RATE = 0.0


@dataclass(frozen=True)
class ContractGreek:
    """Vanna/Charm variant of ContractGreeks — carries IV and DTE per contract."""

    strike: float
    option_right: str
    open_interest: int
    iv: float
    t_years: float
    vanna: float | None = None
    charm: float | None = None


def _dealer_sign(right: str) -> float:
    """Dealer positioning: customer long calls → dealer short (−1); customer short puts → dealer long (+1)."""
    r = (right or "").strip().upper()
    if r in ("C", "CALL"):
        return -1.0
    return 1.0


def strike_vanna_charm_from_contracts(
    contracts: Sequence[ContractGreek],
    spot: float,
) -> list[dict[str, Any]]:
    """Aggregate per-strike dealer Vanna and Charm exposure.

    Units:  contracts × MULTIPLIER × greek  (spot dollars per 1-unit shock).
    Sign:   dealer-oriented (customer long call = dealer short vanna/charm).
    """
    by_strike: dict[float, dict[str, Any]] = {}
    for c in contracts:
        sk = float(c.strike)
        if sk <= 0:
            continue
        bucket = by_strike.setdefault(
            sk,
            {
                "strike": sk,
                "call_oi": 0,
                "put_oi": 0,
                "call_vanna": 0.0,
                "put_vanna": 0.0,
                "call_charm": 0.0,
                "put_charm": 0.0,
                "has_live_vanna": False,
                "has_live_charm": False,
            },
        )
        right = (c.option_right or "").strip().upper()
        if right not in ("C", "CALL", "P", "PUT"):
            continue

        vanna = c.vanna
        if vanna is None:
            vanna = bs_vanna(spot, sk, c.iv, c.t_years, rate=VANNA_CHARM_RATE, option_right=right)
        else:
            bucket["has_live_vanna"] = True

        charm = c.charm
        if charm is None:
            charm = bs_charm(spot, sk, c.iv, c.t_years, rate=VANNA_CHARM_RATE, option_right=right)
        else:
            bucket["has_live_charm"] = True

        sign = _dealer_sign(right)
        signed_vanna = sign * vanna * float(c.open_interest or 0) * MULTIPLIER
        signed_charm = sign * charm * float(c.open_interest or 0) * MULTIPLIER

        if right in ("C", "CALL"):
            bucket["call_oi"] += int(c.open_interest or 0)
            bucket["call_vanna"] += signed_vanna
            bucket["call_charm"] += signed_charm
        else:
            bucket["put_oi"] += int(c.open_interest or 0)
            bucket["put_vanna"] += signed_vanna
            bucket["put_charm"] += signed_charm

    rows: list[dict[str, Any]] = []
    for sk in sorted(by_strike.keys()):
        b = by_strike[sk]
        net_vanna = float(b["call_vanna"]) + float(b["put_vanna"])
        net_charm = float(b["call_charm"]) + float(b["put_charm"])
        source = "oi_bs"
        if b["has_live_vanna"] or b["has_live_charm"]:
            source = "oi_live_greeks"
        rows.append(
            {
                "strike": sk,
                "call_oi": int(b["call_oi"]),
                "put_oi": int(b["put_oi"]),
                "call_vanna": round(float(b["call_vanna"]), 6),
                "put_vanna": round(float(b["put_vanna"]), 6),
                "call_charm": round(float(b["call_charm"]), 6),
                "put_charm": round(float(b["put_charm"]), 6),
                "net_vanna": round(net_vanna, 6),
                "net_charm": round(net_charm, 6),
                "source": source,
            }
        )
    return rows


def zero_crossing_strike(
    distribution: Sequence[Mapping[str, Any]],
    metric: str,
    spot: float,
) -> float | None:
    """Strike where cumulative ``metric`` crosses zero, nearest to ``spot``.

    Same linear-interpolation approach as ``engines.gex.compute_gex_levels``.
    """
    rows = sorted(distribution, key=lambda r: float(r["strike"]))
    if not rows:
        return None
    cum = 0.0
    prev_strike: float | None = None
    prev_cum = 0.0
    best: tuple[float, float] | None = None
    for r in rows:
        sk = float(r["strike"])
        cum += float(r.get(metric) or 0)
        if prev_strike is not None and prev_cum * cum <= 0 and (prev_cum != 0 or cum != 0):
            if cum != prev_cum:
                t = -prev_cum / (cum - prev_cum)
                zg = prev_strike + t * (sk - prev_strike)
            else:
                zg = sk
            dist = abs(zg - spot)
            if best is None or dist < best[1]:
                best = (round(zg, 4), dist)
        prev_strike = sk
        prev_cum = cum
    return best[0] if best is not None else None


def _ensure_contract_greek(c: ContractGreeks, *, iv: float, t_years: float) -> ContractGreek:
    """Adapt a raw ``ContractGreeks`` (from GEX layer) into a ``ContractGreek`` for vanna/charm."""
    return ContractGreek(
        strike=c.strike,
        option_right=c.option_right,
        open_interest=c.open_interest,
        iv=iv,
        t_years=t_years,
        vanna=None,
        charm=None,
    )
