"""Hit-rate gate — Wave Y.3 B3 + C3 (extracted in Wave Z cleanup).

Filter-scoped hit-rate check: when ``policy.min_hit_rate`` and
``policy.flag_filter`` are both set, verify each ``flag_filter`` lens has
``hit_rate_20d`` ≥ ``min_hit_rate`` and ``n > 0`` per the latest
``global_signal_decay_summary``.  A failing gate never aborts the run;
it flags the candidate_batch draft with ``hit_rate_warn`` so Owner can
override in Decision Inbox (Y.3 decision C3).

D10 BLOCKED — the gate only reads decay summaries; it never touches Trade
DB or the IB operator command stream.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness.data_sources import FLAG_TO_DECAY_LENS


def lenses_from_flag_filter(flag_filter: str | None) -> list[str]:
    """Extract lens keys from a ``k:v,k:v`` flag filter.

    ``iv_rank:hot,vrp:hot`` → ``["iv_rank", "vrp"]``.  Order-preserving,
    de-duplicated, lowercased.  Empty/invalid → ``[]``.
    """
    if not flag_filter or not isinstance(flag_filter, str):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in flag_filter.split(","):
        piece = part.strip()
        if not piece or ":" not in piece:
            continue
        key = piece.split(":", 1)[0].strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def apply_hit_rate_gate(
    *,
    policy: dict[str, Any],
    decay_summary: dict[str, dict[str, Any]],
    flag_filter: str | None,
) -> dict[str, Any]:
    """Return a diagnostic dict describing the gate verdict.

    Never blocks the run — the runtime only uses this to decorate the
    candidate_batch draft with ``hit_rate_warn`` when ``applied and not ok``.

    Return shape::

        {
          "applied": bool,             # whether the gate ran at all
          "ok": bool,                  # gate verdict when applied
          "reason": str,               # short human-readable status
          "min_hit_rate": float | None,
          "lens_scoped": [str, ...],   # lenses drawn from flag_filter
          "lens_results": {
             <decay_lens>: {"hit_rate_20d": float|None, "n": int, "pass": bool},
             ...
          },
          "failing": [str, ...],       # mapped decay lenses that failed
          "skipped": [                 # unmapped flags / no decay row
             {"flag_key": str, "reason": "unmapped"|"no_decay_row", ...},
          ],
        }
    """
    result: dict[str, Any] = {
        "applied": False,
        "ok": True,
        "reason": "gate not applied",
        "min_hit_rate": None,
        "lens_scoped": [],
        "lens_results": {},
        "failing": [],
        "skipped": [],
    }
    raw_min = policy.get("min_hit_rate")
    if raw_min is None:
        result["reason"] = "policy.min_hit_rate unset"
        return result
    try:
        min_hr = float(raw_min)
    except (TypeError, ValueError):
        result["reason"] = "policy.min_hit_rate not a number"
        return result

    lens_scoped = lenses_from_flag_filter(flag_filter)
    if not lens_scoped:
        result["reason"] = "policy.flag_filter absent or has no lens keys"
        return result

    result["applied"] = True
    result["min_hit_rate"] = min_hr
    result["lens_scoped"] = lens_scoped

    failing: list[str] = []
    skipped: list[dict[str, Any]] = []
    evaluated = 0
    for flag_key in lens_scoped:
        decay_lens = FLAG_TO_DECAY_LENS.get(flag_key)
        if decay_lens is None:
            skipped.append({"flag_key": flag_key, "reason": "unmapped"})
            continue
        entry = (
            decay_summary.get(decay_lens) if isinstance(decay_summary, dict) else None
        )
        hr20 = None
        n = 0
        if isinstance(entry, dict):
            hr_val = entry.get("hit_rate_20d")
            try:
                hr20 = float(hr_val) if hr_val is not None else None
            except (TypeError, ValueError):
                hr20 = None
            try:
                n = int(entry.get("n") or 0)
            except (TypeError, ValueError):
                n = 0
        if n <= 0:
            skipped.append(
                {
                    "flag_key": flag_key,
                    "decay_lens": decay_lens,
                    "reason": "no_decay_row",
                }
            )
            continue
        evaluated += 1
        passed = hr20 is not None and hr20 >= min_hr
        result["lens_results"][decay_lens] = {
            "hit_rate_20d": hr20,
            "n": n,
            "pass": passed,
            "flag_key": flag_key,
        }
        if not passed:
            failing.append(decay_lens)
    result["skipped"] = skipped
    result["failing"] = failing
    result["ok"] = not failing
    if evaluated == 0:
        result["reason"] = "no mapped decay rows to evaluate"
    elif not failing:
        result["reason"] = "all lenses pass"
    else:
        result["reason"] = f"failing: {failing}"
    return result
