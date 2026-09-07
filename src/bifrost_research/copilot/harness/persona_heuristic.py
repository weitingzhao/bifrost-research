"""Deterministic Persona heuristics — the offline half of the judge stage.

Split out of ``persona_eval`` in B2 (research-loop-automation) when the
two-model judge landed and the module crossed the 800-line ratchet. Nothing
here calls a model: stances are read off SQL evidence plus a read-only
holdings overlay, so CI and offline runs stay reproducible, and a judge that
fails has something honest to fall back to.

D10 BLOCKED — advisory stances only; never places orders.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

STANCES = frozenset({"support", "caution", "oppose", "abstain"})
EVAL_AGENTS = ("analyze", "portfolio", "validate", "verdict")


def _clamp_stance(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    return s if s in STANCES else "abstain"


def _verdict_row(
    agent: str,
    stance: str,
    summary: str,
    *,
    confidence: float | None = None,
    source: str = "heuristic",
    model: str | None = None,
    summary_zh: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "agent": agent,
        "stance": _clamp_stance(stance),
        "summary": (summary or "")[:500],
        "source": source,
    }
    # Written by the same call that wrote the English, so the two say the same
    # thing about the same evidence. Absent on heuristic rows and on every run
    # made before the judges were asked for it, which is why the reader falls
    # back to English rather than seeing a gap.
    if summary_zh and summary_zh.strip():
        row["summary_zh"] = summary_zh.strip()[:500]
    if model:
        row["model"] = model
    if confidence is not None and _is_finite(confidence):
        row["confidence"] = max(0.0, min(1.0, float(confidence)))
    return row


def _is_finite(x: Any) -> bool:
    try:
        return x is not None and float(x) == float(x) and abs(float(x)) != float("inf")
    except (TypeError, ValueError):
        return False


def _symbol_from_position(pos: dict[str, Any]) -> str | None:
    for key in ("symbol", "ticker", "underlying", "localSymbol"):
        raw = pos.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().upper()
    contract = pos.get("contract")
    if isinstance(contract, dict):
        for key in ("symbol", "localSymbol"):
            raw = contract.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip().upper()
    return None


HOLDINGS_SNAPSHOT_TIMEOUT_S = float(
    os.environ.get("BIFROST_HOLDINGS_SNAPSHOT_TIMEOUT_S") or 1.5
)

# How long to trust a failed probe. Only failures are remembered: holdings move,
# so a cached "applied" snapshot would go stale, while a cached "unavailable"
# only costs the overlay it was already not providing.
#
# 60s was too short to help. Runs are minutes apart, so every one of them paid
# the ~5s DNS timeout again — measured at 5.02s on a 5.12s run after the cache
# was in place. 15 minutes covers a working session while still noticing a
# monitor that comes back inside the hour.
HOLDINGS_UNAVAILABLE_TTL_S = float(
    os.environ.get("BIFROST_HOLDINGS_UNAVAILABLE_TTL_S") or 900.0
)

_unavailable_until: float = 0.0


def reset_holdings_probe_cache() -> None:
    """Forget a cached failure — for tests, and for callers that know better."""
    global _unavailable_until
    _unavailable_until = 0.0


def load_held_symbols() -> tuple[set[str] | None, str]:
    """Best-effort read-only holdings via Trade monitor ``/status``.

    Returns ``(symbols, status)`` where ``symbols is None`` means holdings were
    not applied (unavailable / misconfigured). Empty set means snapshot ok but
    no positions. Never writes; D10 untouched.
    """
    global _unavailable_until

    # The Trade monitor is addressed by its in-cluster name. In the cluster that
    # resolves; from a dev machine it does not, and the failure is a DNS one —
    # `getaddrinfo` blocks for ~5s and no HTTP timeout bounds it (measured: a
    # 0.2s budget still took 5.02s). That was 98% of a local run, paid again on
    # every run, to re-learn the same answer. Remember the failure instead.
    if time.monotonic() < _unavailable_until:
        return None, "unavailable"

    try:
        from bifrost_research.mcp.tools._trade_api_client import base_monitor, get
        from bifrost_research.mcp.tools.trade_context import _extract_light_status

        # Best-effort means best-effort. With the default 8s budget an
        # unreachable Trade monitor made this single call the whole run:
        # measured 5.02s of a 5.12s run — 98% — spent waiting to conclude that
        # holdings were unavailable, on every scheduled run and every click.
        # The portfolio persona abstains without it, so a short wait is the
        # correct price for an overlay nothing downstream depends on.
        status = get(base_monitor(), "/status", timeout=HOLDINGS_SNAPSHOT_TIMEOUT_S)
        if not isinstance(status, dict):
            _unavailable_until = time.monotonic() + HOLDINGS_UNAVAILABLE_TTL_S
            return None, "unavailable"
        light = _extract_light_status(status)
        held: set[str] = set()
        for acct in light.get("accounts") or []:
            if not isinstance(acct, dict):
                continue
            for pos in acct.get("positions") or []:
                if not isinstance(pos, dict):
                    continue
                sym = _symbol_from_position(pos)
                if sym:
                    held.add(sym)
        return held, "applied"
    except Exception as exc:  # noqa: BLE001
        logger.info("persona_eval holdings snapshot skipped: %s", str(exc)[:160])
        _unavailable_until = time.monotonic() + HOLDINGS_UNAVAILABLE_TTL_S
        return None, "unavailable"


def _portfolio_heuristic(
    symbol: str,
    *,
    held_symbols: set[str] | None,
    holdings_status: str,
) -> dict[str, Any]:
    if held_symbols is None or holdings_status != "applied":
        return _verdict_row(
            "portfolio",
            "abstain",
            "Holdings not applied — Trade monitor snapshot unavailable "
            "(heuristic path; set BIFROST_PERSONA_EVAL_AGENTS=1 for LLM portfolio tool).",
            confidence=0.3,
        )
    sym = (symbol or "").strip().upper()
    n = len(held_symbols)
    if not held_symbols:
        return _verdict_row(
            "portfolio",
            "caution",
            "Holdings snapshot empty — no open positions to overlay.",
            confidence=0.4,
        )
    if sym and sym in held_symbols:
        return _verdict_row(
            "portfolio",
            "caution",
            f"Already held ({n} symbols in snapshot) — concentration if adding.",
            confidence=0.65,
        )
    return _verdict_row(
        "portfolio",
        "support",
        f"Not in current holdings ({n} symbols) — diversification-friendly on overlay.",
        confidence=0.55,
    )


def heuristic_verdicts_for_item(
    item: dict[str, Any],
    *,
    held_symbols: set[str] | None = None,
    holdings_status: str = "unavailable",
) -> list[dict[str, Any]]:
    """Build analyze/portfolio/validate/verdict stances from evidence + score."""
    ev = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    sel = ev.get("selection") if isinstance(ev.get("selection"), dict) else {}
    rec = ev.get("track_record") if isinstance(ev.get("track_record"), dict) else {}
    score = item.get("score")
    try:
        score_n = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_n = None

    # --- analyze ---
    sepa = sel.get("sepa_score")
    try:
        sepa_n = float(sepa) if sepa is not None else None
    except (TypeError, ValueError):
        sepa_n = None
    if sel.get("status") == "not_measured" or (sepa_n is None and not sel.get("path")):
        analyze = _verdict_row(
            "analyze",
            "abstain",
            "Structure view not measured — insufficient SEPA/path evidence.",
            confidence=0.2,
        )
    elif sepa_n is not None and sepa_n >= 80:
        analyze = _verdict_row(
            "analyze",
            "support",
            f"Strong structure: SEPA {sepa_n:.0f}"
            + (f" · {sel.get('path')}" if sel.get("path") else ""),
            confidence=0.75,
        )
    elif sepa_n is not None and sepa_n < 60:
        analyze = _verdict_row(
            "analyze",
            "oppose",
            f"Weak structure: SEPA {sepa_n:.0f} below constructive band.",
            confidence=0.7,
        )
    else:
        analyze = _verdict_row(
            "analyze",
            "caution",
            "Mixed structure"
            + (f" (SEPA {sepa_n:.0f})" if sepa_n is not None else "")
            + (f" · {sel.get('path')}" if sel.get("path") else ""),
            confidence=0.55,
        )

    # --- portfolio (read-only holdings overlay when Trade monitor reachable) ---
    portfolio = _portfolio_heuristic(
        str(item.get("symbol") or ""),
        held_symbols=held_symbols,
        holdings_status=holdings_status,
    )

    # --- validate ---
    horizons = [
        h
        for h in (rec.get("horizons") or [])
        if isinstance(h, dict) and h.get("hit_rate") is not None
    ]
    if horizons:
        rates = [float(h["hit_rate"]) for h in horizons if _is_finite(h.get("hit_rate"))]
        avg = sum(rates) / len(rates) if rates else None
        if avg is not None and avg < 0.35:
            validate = _verdict_row(
                "validate",
                "oppose",
                f"Settled hit-rate weak (avg {avg:.0%}) — falsification leans against.",
                confidence=0.8,
            )
        elif avg is not None and avg >= 0.55:
            validate = _verdict_row(
                "validate",
                "support",
                f"Settled hit-rate constructive (avg {avg:.0%}).",
                confidence=0.75,
            )
        else:
            validate = _verdict_row(
                "validate",
                "caution",
                "Settled record mixed"
                + (f" (avg {avg:.0%})" if avg is not None else "")
                + ".",
                confidence=0.55,
            )
    else:
        validate = _verdict_row(
            "validate",
            "caution",
            rec.get("reason")
            or "No settled track record yet — treat as unfalsified proposal.",
            confidence=0.4,
        )

    # --- verdict synthesis ---
    votes = {
        "analyze": analyze["stance"],
        "portfolio": portfolio["stance"],
        "validate": validate["stance"],
    }
    if votes["validate"] == "oppose":
        net = "oppose"
        summary = "Net oppose: validate dissent blocks constructive call."
    elif votes["analyze"] == "oppose" and votes["validate"] != "support":
        net = "oppose"
        summary = "Net oppose: structure and validation do not support."
    elif votes["analyze"] == "support" and votes["validate"] in {"support", "caution", "abstain"}:
        net = "support" if votes["validate"] == "support" else "caution"
        summary = (
            "Net support: structure constructive; validation aligned."
            if net == "support"
            else "Net caution: structure ok but validation not fully confirming."
        )
    else:
        net = "caution"
        summary = "Net caution: insufficient agreement across specialists."

    if score_n is not None:
        summary = f"{summary} Score={score_n:.1f}."

    verdict = _verdict_row("verdict", net, summary, confidence=0.65)
    return [analyze, portfolio, validate, verdict]


def net_stance_from_verdicts(verdicts: list[dict[str, Any]]) -> str:
    for v in verdicts:
        if v.get("agent") == "verdict":
            return _clamp_stance(str(v.get("stance")))
    return "abstain"


def validate_stance(verdicts: list[dict[str, Any]]) -> str:
    for v in verdicts:
        if v.get("agent") == "validate":
            return _clamp_stance(str(v.get("stance")))
    return "abstain"


__all__ = [
    "EVAL_AGENTS",
    "HOLDINGS_SNAPSHOT_TIMEOUT_S",
    "HOLDINGS_UNAVAILABLE_TTL_S",
    "STANCES",
    "heuristic_verdicts_for_item",
    "load_held_symbols",
    "net_stance_from_verdicts",
    "reset_holdings_probe_cache",
    "validate_stance",
]
