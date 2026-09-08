"""The leash — what an unattended run may accept on its own (research-loop-automation D3).

A candidate passes the gate when, from the run's own record,

  1. the judges agree        — every model reached the same verdict (``agreement == "agree"``);
                               a single judge, a fallback or a split holds the name
  2. validate did not block  — no validate persona opposed, and the net stance is support / caution
  3. the evidence is present — the selection block is measured (the setup it was chosen on can be restated)
  4. the source has a record — the source's settled hit rate at its longest judged horizon is known,
                               rests on enough outcomes, and clears ``min_source_hit_rate``

Everything else stays for the Owner, with the reason on the draft. D10 BLOCKED —
accepting a candidate means a hypothesis, never an order.
"""

from __future__ import annotations

from typing import Any

DEFAULT_MIN_SOURCE_HIT_RATE = 0.45
# A hit rate from fewer settled outcomes than this is not a record yet.
MIN_SOURCE_JUDGED = 5
ACCEPTING_STANCES = frozenset({"support", "caution"})


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def source_record(track_record: dict[str, Any] | None) -> tuple[float | None, int, int | None]:
    """``(hit_rate, judged, horizon_days)`` at the longest judged horizon, or ``(None, 0, None)``."""
    best: tuple[float | None, int, int | None] = (None, 0, None)
    for h in _dict(track_record).get("horizons") or []:
        if not isinstance(h, dict):
            continue
        judged = int(h.get("judged") or 0)
        rate = h.get("hit_rate")
        if judged <= 0 or rate is None:
            continue
        horizon = int(h.get("horizon_days") or 0)
        if best[2] is None or horizon > best[2]:
            best = (float(rate), judged, horizon)
    return best


def accept_gate(
    item: dict[str, Any],
    *,
    min_source_hit_rate: float = DEFAULT_MIN_SOURCE_HIT_RATE,
) -> dict[str, Any]:
    """One candidate against the four conditions; ``reasons`` names every failed one."""
    evidence = _dict(item.get("evidence"))
    reasons: list[str] = []

    agreement = item.get("agreement") or evidence.get("agreement")
    if agreement != "agree":
        # "single" and "none" mean a judge never answered, which is a spent
        # purse or a timeout far more often than a split opinion. Naming it
        # keeps a budget event from reading as a disagreement.
        absent = [a for a in (evidence.get("absent_judges") or []) if isinstance(a, dict)]
        if absent:
            who = "; ".join(f"{a.get('model')} — {a.get('reason')}" for a in absent)
            reasons.append(f"a judge did not answer ({who})")
        else:
            reasons.append(f"judges did not agree ({agreement or 'no judge record'})")

    net = item.get("net_stance") or evidence.get("net_stance")
    if item.get("blocked_by_validate"):
        reasons.append("validate blocked")
    if net not in ACCEPTING_STANCES:
        reasons.append(f"net stance {net or 'none'}")

    selection = _dict(evidence.get("selection"))
    if selection.get("status") != "ok":
        reasons.append("selection evidence not measured")

    track = _dict(evidence.get("track_record"))
    rate, judged, horizon = source_record(track)
    if rate is None:
        reasons.append("source track record not measured")
    elif judged < MIN_SOURCE_JUDGED:
        reasons.append(f"source record thin ({judged} judged at T+{horizon})")
    elif rate < min_source_hit_rate:
        reasons.append(f"source hit rate {rate:.0%} < {min_source_hit_rate:.0%} at T+{horizon}")

    return {
        "id": item.get("id"),
        "symbol": str(item.get("symbol") or "").upper(),
        "accept": not reasons,
        "reasons": reasons,
        "agreement": agreement,
        "net_stance": net,
        "source_hit_rate": rate,
        "source_judged": judged,
        "source_horizon_days": horizon,
    }


def split_batch(
    items: list[dict[str, Any]],
    *,
    min_source_hit_rate: float = DEFAULT_MIN_SOURCE_HIT_RATE,
) -> dict[str, Any]:
    """The batch as the leash sees it: who passes, who stays, and why."""
    # A row without an id and a symbol cannot be promoted or held by name; it is not a candidate.
    gates = [
        accept_gate(i, min_source_hit_rate=min_source_hit_rate)
        for i in items
        if isinstance(i, dict) and i.get("id") and i.get("symbol")
    ]
    accepted = [g for g in gates if g["accept"]]
    held = [g for g in gates if not g["accept"]]
    return {
        "min_source_hit_rate": min_source_hit_rate,
        "accepted": [{"id": g["id"], "symbol": g["symbol"]} for g in accepted],
        "held": [{"id": g["id"], "symbol": g["symbol"], "reasons": g["reasons"]} for g in held],
        "accepted_ids": {str(g["id"]) for g in accepted if g["id"]},
    }
