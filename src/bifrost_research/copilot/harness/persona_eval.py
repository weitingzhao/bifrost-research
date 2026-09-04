"""Per-candidate Persona evaluation chain — Policy × Personas E2E Wave 1.

Order (fixed): analyze → portfolio → validate → verdict.

Default path is **deterministic heuristics** from SQL evidence so CI / offline
runs stay reproducible. Optional headless LLM agents activate when
``BIFROST_PERSONA_EVAL_AGENTS=1`` (fail-soft → abstain on timeout).

D10 BLOCKED — advisory stances only; never places orders.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

STANCES = frozenset({"support", "caution", "oppose", "abstain"})
EVAL_AGENTS = ("analyze", "portfolio", "validate", "verdict")

DEFAULT_TIMEOUT_S = float(os.environ.get("BIFROST_PERSONA_EVAL_TIMEOUT_S", "90"))
PER_SYMBOL_TIMEOUT_S = float(os.environ.get("BIFROST_PERSONA_EVAL_SYMBOL_TIMEOUT_S", "45"))


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def agents_enabled() -> bool:
    if _env_flag("BIFROST_PERSONA_EVAL_SKIP_AGENT"):
        return False
    return _env_flag("BIFROST_PERSONA_EVAL_AGENTS")


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
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "agent": agent,
        "stance": _clamp_stance(stance),
        "summary": (summary or "")[:500],
        "source": source,
    }
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


def load_held_symbols() -> tuple[set[str] | None, str]:
    """Best-effort read-only holdings via Trade monitor ``/status``.

    Returns ``(symbols, status)`` where ``symbols is None`` means holdings were
    not applied (unavailable / misconfigured). Empty set means snapshot ok but
    no positions. Never writes; D10 untouched.
    """
    try:
        from bifrost_research.mcp.tools._trade_api_client import base_monitor, get
        from bifrost_research.mcp.tools.trade_context import _extract_light_status

        status = get(base_monitor(), "/status")
        if not isinstance(status, dict):
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


def _parse_json_blob(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


async def _run_verdict_agent_async(
    *,
    prompt: str,
    model_id: str,
    owner_id: str,
    mcp_url: str,
) -> str:
    from agents import Runner
    from agents.mcp import MCPServerSse

    from bifrost_research.copilot.agents.graph import build_eval_verdict_agent

    server = MCPServerSse(
        params={"url": mcp_url},
        cache_tools_list=True,
        name="research-mcp-persona-eval",
        client_session_timeout_seconds=min(60.0, PER_SYMBOL_TIMEOUT_S),
    )
    agent = build_eval_verdict_agent(model_id, mcp=server, owner_id=owner_id)
    async with server:
        import asyncio

        result = await asyncio.wait_for(
            Runner.run(agent, input=prompt, max_turns=8),
            timeout=PER_SYMBOL_TIMEOUT_S,
        )
    final = getattr(result, "final_output", None)
    return str(final) if final is not None else ""


def _agent_verdicts_for_symbol(
    item: dict[str, Any],
    *,
    model_id: str,
    owner_id: str,
    mcp_url: str,
    held_symbols: set[str] | None = None,
    holdings_status: str = "unavailable",
) -> list[dict[str, Any]]:
    """Ask eval verdict agent for JSON stances; fall back to heuristic."""
    import asyncio

    from bifrost_research.copilot.curator.mcp_local import ensure_local_mcp_url

    sym = str(item.get("symbol") or "")
    evidence = item.get("evidence") or {}
    prompt = (
        "You are evaluating one research candidate for an Owner (D10: advisory only).\n"
        "Call analyze_specialist, portfolio_specialist, and validate_specialist as tools "
        "if helpful, then reply with ONLY JSON:\n"
        '{"analyze":{"stance":"support|caution|oppose|abstain","summary":"..."},'
        '"portfolio":{...},"validate":{...},"verdict":{...}}\n'
        f"Symbol: {sym}\nScore: {item.get('score')}\n"
        f"Evidence JSON: {json.dumps(evidence)[:4000]}\n"
    )
    url = mcp_url or os.environ.get("RESEARCH_MCP_SSE_URL") or ensure_local_mcp_url()
    try:
        text = asyncio.run(
            _run_verdict_agent_async(
                prompt=prompt, model_id=model_id, owner_id=owner_id, mcp_url=url
            )
        )
        parsed = _parse_json_blob(text)
        if not parsed:
            raise ValueError("no JSON stance payload")
        out: list[dict[str, Any]] = []
        for agent in ("analyze", "portfolio", "validate", "verdict"):
            block = parsed.get(agent) if isinstance(parsed.get(agent), dict) else {}
            out.append(
                _verdict_row(
                    agent,
                    str(block.get("stance") or "abstain"),
                    str(block.get("summary") or text[:200]),
                    source="agent",
                )
            )
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("persona agent eval failed for %s: %s", sym, exc)
        rows = heuristic_verdicts_for_item(
            item,
            held_symbols=held_symbols,
            holdings_status=holdings_status,
        )
        for r in rows:
            r["source"] = "heuristic_fallback"
            r["agent_error"] = str(exc)[:200]
        return rows


def evaluate_candidates(
    items: list[dict[str, Any]],
    *,
    policy: dict[str, Any] | None = None,
    owner_id: str = "owner",
    model_id: str | None = None,
) -> dict[str, Any]:
    """Attach ``agent_verdicts`` (+ flags) onto each item; return trace summary."""
    policy = policy or {}
    require_validate_pass = policy.get("require_validate_pass", True)
    if isinstance(require_validate_pass, str):
        require_validate_pass = require_validate_pass.strip().lower() in ("1", "true", "yes")

    use_agents = agents_enabled()
    mid = model_id or os.environ.get("BIFROST_PERSONA_EVAL_MODEL") or os.environ.get(
        "BIFROST_CURATOR_MODEL", "deepseek-chat"
    )
    mcp_url = os.environ.get("RESEARCH_MCP_SSE_URL", "")
    held_symbols, holdings_status = load_held_symbols()

    per_symbol: list[dict[str, Any]] = []
    blocked = 0
    fallback_count = 0
    for item in items:
        if use_agents:
            verdicts = _agent_verdicts_for_symbol(
                item,
                model_id=mid,
                owner_id=owner_id,
                mcp_url=mcp_url,
                held_symbols=held_symbols,
                holdings_status=holdings_status,
            )
        else:
            verdicts = heuristic_verdicts_for_item(
                item,
                held_symbols=held_symbols,
                holdings_status=holdings_status,
            )

        if any(v.get("source") == "heuristic_fallback" for v in verdicts):
            fallback_count += 1

        net = net_stance_from_verdicts(verdicts)
        v_stance = validate_stance(verdicts)
        blocked_by_validate = bool(require_validate_pass and v_stance == "oppose")
        if blocked_by_validate:
            blocked += 1

        ev = item.get("evidence")
        if not isinstance(ev, dict):
            ev = {}
            item["evidence"] = ev
        ev["agent_verdicts"] = verdicts
        ev["net_stance"] = net
        item["blocked_by_validate"] = blocked_by_validate
        item["net_stance"] = net

        per_symbol.append(
            {
                "symbol": item.get("symbol"),
                "net_stance": net,
                "validate_stance": v_stance,
                "blocked_by_validate": blocked_by_validate,
                "verdicts": verdicts,
            }
        )

    eligible = [
        i
        for i in items
        if not i.get("blocked_by_validate") and i.get("net_stance") in {"support", "caution"}
    ]
    auto_approve_eligible = len(items) > 0 and blocked == 0 and all(
        (i.get("net_stance") in {"support", "caution"}) for i in items
    )

    mode = "agent" if use_agents else "heuristic"
    return {
        "status": "completed",
        "mode": mode,
        "fallback_used": bool(use_agents and fallback_count > 0),
        "fallback_count": fallback_count,
        "holdings_status": holdings_status,
        "holdings_count": len(held_symbols) if held_symbols is not None else None,
        "require_validate_pass": require_validate_pass,
        "symbols_evaluated": len(items),
        "blocked_by_validate": blocked,
        "auto_approve_eligible": auto_approve_eligible,
        "eligible_count": len(eligible),
        "per_symbol": per_symbol,
    }


__all__ = [
    "EVAL_AGENTS",
    "STANCES",
    "agents_enabled",
    "evaluate_candidates",
    "heuristic_verdicts_for_item",
    "load_held_symbols",
    "net_stance_from_verdicts",
    "validate_stance",
]
