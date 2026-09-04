"""Discover assist at Policy funnel exit — Wave 3.

Does **not** replace ``resolve_universe``. After the funnel returns symbols,
optionally boost / veto before ``max_candidates`` truncation.

Fail-soft: any error → identity (pure Policy result).

D10 BLOCKED — advisory ranking only.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _assist_cfg(policy: dict[str, Any] | None) -> dict[str, Any]:
    raw = (policy or {}).get("discovery_assist")
    if isinstance(raw, bool):
        return {"enabled": raw, "max_veto_fraction": 0.35}
    if not isinstance(raw, dict):
        return {"enabled": False, "max_veto_fraction": 0.35}
    enabled = bool(raw.get("enabled", False))
    try:
        frac = float(raw.get("max_veto_fraction", 0.35))
    except (TypeError, ValueError):
        frac = 0.35
    frac = max(0.0, min(0.9, frac))
    return {"enabled": enabled, "max_veto_fraction": frac}


def _tokens_from_rules(rules: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Extract boost / veto ticker tokens from playbook rule text (best-effort)."""
    boost: set[str] = set()
    veto: set[str] = set()
    ticker_re = re.compile(r"\b([A-Z]{1,5})\b")
    for rule in rules:
        text = " ".join(
            str(rule.get(k) or "")
            for k in ("title", "body", "rule_text", "content", "summary")
        ).upper()
        lower = text.lower()
        found = set(ticker_re.findall(text))
        # Drop common English noise
        found -= {"A", "THE", "AND", "OR", "FOR", "TO", "IN", "ON", "OF", "IF", "SEPA", "VRP", "GEX", "IV"}
        if any(w in lower for w in ("veto", "avoid", "exclude", "blacklist", "never")):
            veto |= found
        if any(w in lower for w in ("boost", "prefer", "favor", "nominate", "watch")):
            boost |= found
    return boost, veto


def apply_discovery_assist(
    symbols: list[str],
    *,
    policy: dict[str, Any] | None = None,
    playbook_rules: list[dict[str, Any]] | None = None,
    row_meta_by_symbol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reorder / filter symbols; return ``{symbols, boost, veto, notes, funnel_step}``."""
    cfg = _assist_cfg(policy)
    original = list(symbols)
    if not cfg["enabled"] or not original:
        return {
            "symbols": original,
            "boost": [],
            "veto": [],
            "notes": ["discovery_assist disabled or empty universe"],
            "enabled": False,
            "funnel_step": {
                "name": "discovery_assist",
                "in_count": len(original),
                "out_count": len(original),
                "filter": "disabled",
                "optional": True,
                "skipped": True,
                "skip_reason": "discovery_assist.enabled=false",
            },
        }

    try:
        boost_set, veto_set = _tokens_from_rules(playbook_rules or [])
        # Prefer high-score meta as soft boost when no rule tokens
        meta = row_meta_by_symbol or {}
        scored: list[tuple[float, str]] = []
        for sym in original:
            m = meta.get(sym) if isinstance(meta.get(sym), dict) else {}
            score = 0.0
            for key in ("sepa_score", "composite_score", "momentum_score", "score"):
                if m.get(key) is not None:
                    try:
                        score = float(m[key])
                        break
                    except (TypeError, ValueError):
                        continue
            if sym in boost_set:
                score += 1000.0
            scored.append((score, sym))

        max_veto = int(len(original) * float(cfg["max_veto_fraction"]))
        vetoed: list[str] = []
        kept: list[str] = []
        for _sc, sym in sorted(scored, key=lambda t: (-t[0], t[1])):
            if sym in veto_set and len(vetoed) < max_veto:
                vetoed.append(sym)
                continue
            kept.append(sym)

        boost_applied = [s for s in kept if s in boost_set]
        notes = [
            f"boost={len(boost_applied)} veto={len(vetoed)} "
            f"(max_veto_fraction={cfg['max_veto_fraction']})"
        ]
        return {
            "symbols": kept,
            "boost": boost_applied,
            "veto": vetoed,
            "notes": notes,
            "enabled": True,
            "funnel_step": {
                "name": "discovery_assist",
                "in_count": len(original),
                "out_count": len(kept),
                "filter": f"boost={len(boost_applied)} veto={len(vetoed)}",
                "dropped_sample": vetoed[:8],
                "optional": True,
                "skipped": False,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("discovery_assist failed: %s", exc)
        return {
            "symbols": original,
            "boost": [],
            "veto": [],
            "notes": [f"fail-soft: {exc}"],
            "enabled": True,
            "error": str(exc)[:200],
            "funnel_step": {
                "name": "discovery_assist",
                "in_count": len(original),
                "out_count": len(original),
                "filter": "error — identity passthrough",
                "optional": True,
                "skipped": True,
                "skip_reason": str(exc)[:120],
            },
        }


__all__ = ["apply_discovery_assist"]
