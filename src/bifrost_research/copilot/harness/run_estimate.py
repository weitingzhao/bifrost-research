"""What the next run of an objective will cost, from what its own runs cost.

The Owner asked to be told the price before pressing the button. The honest way
to answer is not a constant in the code: it is this objective's own history,
because the rate depends on how much evidence its candidates carry and how many
tool rounds its judges take on them.

Judging is close to linear in candidates. Two runs measured on 2026-09-07 —
eight candidates and three — put deepseek-chat at $0.0694 and $0.0667 a
candidate and gpt-4o-mini at $0.0095 and $0.0104. So a rate per candidate,
averaged over recent runs of the same objective, projects well.

Every figure returned carries the number of runs behind it. An estimate with one
run behind it is a guess and must say so, because the reader is about to spend
real money on the strength of it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Runs of this objective to average. Far enough back to smooth tool-round
#: variance, near enough that a policy change is not averaged with its past.
DEFAULT_LOOKBACK = 10

#: Used only when an objective has never run with judges. Measured, not guessed
#: — but from other objectives, which is why `source` says so.
FALLBACK_RATES: dict[str, float] = {"deepseek": 0.0680, "openai": 0.0100}
FALLBACK_TRIAGE_USD = 0.0002


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [r for r in value if isinstance(r, dict)]


def _outputs(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return _mapping(json.loads(raw))
        except ValueError:
            return {}
    return {}


def observed_rates(runs: list[Any]) -> dict[str, dict[str, Any]]:
    """Per-model dollars per candidate, from runs that actually judged.

    A run whose judges never ran, or that judged nothing, carries no rate and is
    skipped rather than counted as free — averaging zeros in would understate
    the next run by exactly the fraction of runs that failed.
    """
    acc: dict[str, dict[str, float]] = {}
    for raw in runs:
        out = _outputs(raw)
        persona = _mapping(out.get("persona_eval"))
        judged = persona.get("symbols_evaluated")
        try:
            judged = int(judged)
        except (TypeError, ValueError):
            continue
        if judged <= 0:
            continue
        for m in _rows(persona.get("models")):
            model = str(m.get("model") or "").strip()
            cost = m.get("cost_usd")
            if not model or not isinstance(cost, (int, float)) or cost <= 0:
                continue
            slot = acc.setdefault(model, {"cost": 0.0, "candidates": 0.0, "runs": 0.0})
            slot["cost"] += float(cost)
            slot["candidates"] += judged
            slot["runs"] += 1
    return {
        model: {
            "usd_per_candidate": round(v["cost"] / v["candidates"], 6),
            "runs": int(v["runs"]),
            "candidates": int(v["candidates"]),
        }
        for model, v in acc.items()
        if v["candidates"] > 0
    }


def observed_triage_usd(runs: list[Any]) -> float | None:
    """Mean cost of one triage call, or None when none has been recorded."""
    costs = [
        float(c)
        for raw in runs
        for c in [_mapping(_outputs(raw).get("triage")).get("cost_usd")]
        if isinstance(c, (int, float)) and c > 0
    ]
    return round(sum(costs) / len(costs), 6) if costs else None


def _provider_of(model: str) -> str:
    lower = model.lower()
    if lower.startswith("deepseek"):
        return "deepseek"
    if lower.startswith(("gpt", "openai")):
        return "openai"
    return "other"


def estimate_run(
    *,
    runs: list[Any],
    models: list[str],
    candidates: int,
    triage: bool = True,
) -> dict[str, Any]:
    """Project the next run's judge spend for ``candidates`` names.

    Returns the total, one line per model, and how the rate was arrived at, so
    the caller can show a figure the reader is able to disbelieve.
    """
    rates = observed_rates(runs)
    lines: list[dict[str, Any]] = []
    total = 0.0
    for model in models:
        seen = rates.get(model)
        if seen:
            rate, source, n = seen["usd_per_candidate"], "measured", seen["runs"]
        else:
            rate = FALLBACK_RATES.get(_provider_of(model), FALLBACK_RATES["openai"])
            source, n = "typical", 0
        cost = round(rate * max(0, candidates), 6)
        total += cost
        lines.append(
            {
                "model": model,
                "usd_per_candidate": rate,
                "usd": cost,
                "source": source,
                "runs": n,
            }
        )
    triage_usd = 0.0
    if triage:
        triage_usd = observed_triage_usd(runs) or FALLBACK_TRIAGE_USD
        total += triage_usd
    return {
        "candidates": max(0, candidates),
        "models": lines,
        "triage_usd": round(triage_usd, 6),
        "total_usd": round(total, 6),
        # "measured" only when every model's rate came from this objective's own
        # history. One borrowed rate makes the whole figure an approximation, and
        # calling it measured would overstate what is known.
        "source": "measured"
        if lines and all(line["source"] == "measured" for line in lines)
        else "typical",
        "runs_sampled": max((line["runs"] for line in lines), default=0),
    }


def estimate_summary(est: dict[str, Any]) -> str:
    """One sentence a reader can act on."""
    n = est.get("candidates") or 0
    total = est.get("total_usd") or 0.0
    head = f"about ${total:.2f} to judge {n} candidate{'' if n == 1 else 's'}"
    if est.get("source") == "measured":
        return f"{head}, from this objective's last {est.get('runs_sampled')} run(s)"
    return f"{head}, from typical rates — this objective has no judged run to measure"


__all__ = [
    "DEFAULT_LOOKBACK",
    "estimate_run",
    "estimate_summary",
    "observed_rates",
    "observed_triage_usd",
]
