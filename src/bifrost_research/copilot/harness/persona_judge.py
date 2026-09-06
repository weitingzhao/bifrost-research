"""The two-model judge — B2 of research-loop-automation (D-RLA-3).

In agent mode every candidate is judged by **every model in
``PERSONA_EVAL_MODELS``** (default DeepSeek chat and OpenAI gpt-4o-mini,
concurrently). ``consensus`` reduces the opinions: the batch's stance on a
symbol is the stance the judges agree on; anything else is ``dissent``. A judge
that failed and fell back to the heuristic is a dissent, never an agreement —
an unattended auto-accept needs two real opinions, not one opinion and a
default. Each provider has its own daily purse (``rate_limit``); today's spend
is replayed from ``research.ai_action_log`` at the start of a run so the cap
holds across Cron pods, and this run's spend is written back as one ledger row
per model.

D10 BLOCKED — advisory stances only; never places orders.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from bifrost_research.copilot import rate_limit
from bifrost_research.copilot.harness.persona_heuristic import (
    EVAL_AGENTS,
    _clamp_stance,
    _verdict_row,
    heuristic_verdicts_for_item,
    net_stance_from_verdicts,
    validate_stance,
)
from bifrost_research.copilot.models import ModelConfigError, resolve_chat_endpoint
from bifrost_research.copilot.providers import estimate_cost
from bifrost_research.db.conn import rollback_quietly
from bifrost_research.repositories import ai_action_log as action_repo

logger = logging.getLogger(__name__)

PER_SYMBOL_TIMEOUT_S = float(os.environ.get("BIFROST_PERSONA_EVAL_SYMBOL_TIMEOUT_S", "45"))

# The judges. Two providers by default so one outage, one bad day of one
# model, or one exhausted purse cannot pass as agreement.
DEFAULT_EVAL_MODELS = "deepseek-chat,gpt-4o-mini"
# One ledger row per model per run; the daily per-provider cap is rebuilt from
# today's rows of this kind.
SPEND_ACTION_KIND = "persona_eval_spend"
# The stance that wins when judges disagree on validate: the more severe one.
_SEVERITY = {"support": 0, "abstain": 1, "caution": 2, "oppose": 3}
DISSENT = "dissent"


def eval_models(model_id: str | None = None) -> list[str]:
    """The judges, in order, de-duplicated.

    An explicit ``model_id`` (legacy single-model callers) wins; then
    ``PERSONA_EVAL_MODELS``; then the older single-model env names; then the
    two-provider default.
    """
    raw = (
        (model_id or "").strip()
        or os.environ.get("PERSONA_EVAL_MODELS", "").strip()
        or os.environ.get("BIFROST_PERSONA_EVAL_MODEL", "").strip()
        or os.environ.get("BIFROST_CURATOR_MODEL", "").strip()
        or DEFAULT_EVAL_MODELS
    )
    out: list[str] = []
    for part in raw.split(","):
        model = part.strip()
        if model and model not in out:
            out.append(model)
    return out or [DEFAULT_EVAL_MODELS.split(",")[0]]


def provider_of(model: str) -> str:
    try:
        return resolve_chat_endpoint(model).provider
    except ModelConfigError:
        return (model.split("-", 1)[0] or "unknown").lower()


def most_severe(stances: list[str]) -> str:
    if not stances:
        return "abstain"
    return max((_clamp_stance(s) for s in stances), key=lambda s: _SEVERITY[s])


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


def _eval_prompt(item: dict[str, Any]) -> str:
    sym = str(item.get("symbol") or "")
    evidence = item.get("evidence") or {}
    return (
        "You are evaluating one research candidate for an Owner (D10: advisory only).\n"
        "Call analyze_specialist, portfolio_specialist, and validate_specialist as tools "
        "if helpful, then reply with ONLY one JSON object:\n"
        '{"analyze":{"stance":"support|caution|oppose|abstain","summary":"..."},'
        '"portfolio":{...},"validate":{...},"verdict":{...}}\n'
        "All four keys are required in your final message — analyze, portfolio, validate "
        "and verdict — each with a stance and a one-sentence summary. Do not return a "
        "specialist's output as your own answer; a reply missing any key is discarded as a "
        "failed judgement. If a specialist tool errors, say so in that block's summary and "
        "give the stance you can defend from the evidence (abstain is acceptable).\n"
        f"Symbol: {sym}\nScore: {item.get('score')}\n"
        f"Evidence JSON: {json.dumps(evidence)[:4000]}\n"
    )


async def _run_verdict_agent_async(
    *,
    prompt: str,
    model_id: str,
    owner_id: str,
    mcp_url: str,
) -> tuple[str, dict[str, int]]:
    """Run the verdict agent once; return its final text and token usage."""
    import asyncio

    from agents import Runner
    from agents.mcp import MCPServerSse

    from bifrost_research.copilot.agents.graph import build_eval_verdict_agent

    server = MCPServerSse(
        params={"url": mcp_url},
        cache_tools_list=True,
        name=f"research-mcp-persona-eval-{model_id}",
        client_session_timeout_seconds=min(60.0, PER_SYMBOL_TIMEOUT_S),
    )
    agent = build_eval_verdict_agent(model_id, mcp=server, owner_id=owner_id)
    async with server:
        result = await asyncio.wait_for(
            Runner.run(agent, input=prompt, max_turns=8),
            timeout=PER_SYMBOL_TIMEOUT_S,
        )
    final = getattr(result, "final_output", None)
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    tokens = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    }
    return (str(final) if final is not None else ""), tokens


def _new_call(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "provider": provider_of(model),
        "ok": False,
        "fallback": False,
        "cap_exceeded": False,
        "elapsed_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "error": None,
    }


async def _agent_verdicts_for_model_async(
    *,
    prompt: str,
    model_id: str,
    owner_id: str,
    mcp_url: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One judge on one symbol. Never raises: a failure is a call record with
    ``ok=False`` and an ``error``, and no rows."""
    call = _new_call(model_id)
    started = time.perf_counter()
    try:
        text, tokens = await _run_verdict_agent_async(
            prompt=prompt, model_id=model_id, owner_id=owner_id, mcp_url=mcp_url
        )
        call["input_tokens"] = tokens.get("input_tokens", 0)
        call["output_tokens"] = tokens.get("output_tokens", 0)
        call["cost_usd"] = round(
            estimate_cost(model_id, call["input_tokens"], call["output_tokens"]), 6
        )
        parsed = _parse_json_blob(text)
        if not parsed:
            raise ValueError("no JSON stance payload")
        # A judge that hands back only its analyst's block is not a verdict.
        # On the first DEV run gpt-4o-mini returned {"analyze": …} alone and
        # the missing blocks read as three abstains — a non-answer dressed as
        # an opinion. Missing blocks are a failed judgement, which is dissent.
        missing = [agent for agent in EVAL_AGENTS if not isinstance(parsed.get(agent), dict)]
        if missing:
            raise ValueError(f"incomplete JSON (missing: {', '.join(missing)})")
        rows: list[dict[str, Any]] = []
        for agent in EVAL_AGENTS:
            block = parsed.get(agent) if isinstance(parsed.get(agent), dict) else {}
            rows.append(
                _verdict_row(
                    agent,
                    str(block.get("stance") or "abstain"),
                    str(block.get("summary") or text[:200]),
                    source="agent",
                    model=model_id,
                )
            )
        call["ok"] = True
        return rows, call
    except TimeoutError:
        # asyncio's TimeoutError carries no message; an empty error read as
        # "judge failed" for no reason on the first DEV run.
        call["error"] = f"timeout after {PER_SYMBOL_TIMEOUT_S:.0f}s"
        return [], call
    except Exception as exc:  # noqa: BLE001
        call["error"] = (str(exc) or type(exc).__name__)[:200]
        return [], call
    finally:
        call["elapsed_ms"] = int((time.perf_counter() - started) * 1000)


def _fallback_rows(
    item: dict[str, Any],
    *,
    model: str | None,
    error: str,
    held_symbols: set[str] | None,
    holdings_status: str,
) -> list[dict[str, Any]]:
    rows = heuristic_verdicts_for_item(
        item, held_symbols=held_symbols, holdings_status=holdings_status
    )
    for r in rows:
        r["source"] = "heuristic_fallback"
        r["agent_error"] = error[:200]
        if model:
            r["model"] = model
    return rows


def _judge_symbol(
    item: dict[str, Any],
    *,
    models: list[str],
    owner_id: str,
    mcp_url: str,
    held_symbols: set[str] | None,
    holdings_status: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every judge on one symbol, concurrently.

    Returns ``(rows, calls)``: the verdict rows of every model (each tagged with
    its ``model``; a failed model contributes heuristic rows marked
    ``heuristic_fallback``) and one call record per model in configured order.
    A model whose provider purse is empty is not called at all — the record
    says so, and it counts as a fallback.
    """
    import asyncio

    from bifrost_research.copilot.curator.mcp_local import ensure_local_mcp_url

    prompt = _eval_prompt(item)
    url = mcp_url or os.environ.get("RESEARCH_MCP_SSE_URL") or ensure_local_mcp_url()

    calls: dict[str, dict[str, Any]] = {}
    planned: list[str] = []
    for model in models:
        provider = provider_of(model)
        if rate_limit.provider_remaining_usd(provider) <= 0:
            call = _new_call(model)
            call["cap_exceeded"] = True
            call["error"] = (
                f"daily cap reached for {provider} "
                f"({rate_limit.provider_cap_env(provider)}="
                f"{rate_limit.provider_cap_usd(provider):.2f})"
            )
            calls[model] = call
        else:
            planned.append(model)

    async def _gather_judges() -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
        return await asyncio.gather(
            *[
                _agent_verdicts_for_model_async(
                    prompt=prompt, model_id=m, owner_id=owner_id, mcp_url=url
                )
                for m in planned
            ]
        )

    results = asyncio.run(_gather_judges()) if planned else []

    rows_by_model: dict[str, list[dict[str, Any]]] = {}
    for rows, call in results:
        calls[call["model"]] = call
        if call["ok"]:
            rate_limit.record_provider_usage(
                call["provider"],
                tokens=call["input_tokens"] + call["output_tokens"],
                cost_usd=call["cost_usd"],
            )
            rows_by_model[call["model"]] = rows

    ordered: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for model in models:
        call = calls[model]
        if not call["ok"]:
            call["fallback"] = True
            logger.warning(
                "persona judge %s failed for %s: %s",
                model,
                item.get("symbol"),
                call["error"],
            )
            rows_by_model[model] = _fallback_rows(
                item,
                model=model,
                error=str(call["error"] or "judge failed"),
                held_symbols=held_symbols,
                holdings_status=holdings_status,
            )
        ordered.append(call)
        all_rows.extend(rows_by_model[model])
    return all_rows, ordered


def consensus(
    rows_by_model: dict[str, list[dict[str, Any]]],
    *,
    fallback_models: set[str] | None = None,
) -> dict[str, Any]:
    """What the judges agree on.

    ``net_stance`` is the verdict every judge reached, else ``dissent``.
    ``validate_stance`` is the most severe validate stance across judges — one
    block is a block. A judge that fell back is a dissent, never an agreement.
    A single judge is ``single``: its stance stands, but it is not agreement.
    """
    fallback_models = fallback_models or set()
    by_model: dict[str, dict[str, str]] = {}
    for model, rows in rows_by_model.items():
        by_model[model] = {
            "net": net_stance_from_verdicts(rows),
            "validate": validate_stance(rows),
        }
    nets = [v["net"] for v in by_model.values()]
    validate = most_severe([v["validate"] for v in by_model.values()])
    if len(by_model) <= 1:
        model = next(iter(by_model), None)
        if model is None:
            agreement, net = "single", "abstain"
        elif model in fallback_models:
            agreement, net = DISSENT, DISSENT
        else:
            agreement, net = "single", nets[0]
    elif fallback_models & set(by_model) or len(set(nets)) > 1:
        agreement, net = DISSENT, DISSENT
    else:
        agreement, net = "agree", nets[0]
    return {
        "net_stance": net,
        "validate_stance": validate,
        "agreement": agreement,
        "by_model": by_model,
    }


def _group_rows_by_model(rows: list[dict[str, Any]], models: list[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {m: [] for m in models}
    for r in rows:
        grouped.setdefault(str(r.get("model") or models[0]), []).append(r)
    return {m: v for m, v in grouped.items() if v}


def _seed_spend_from_ledger(conn: Any, providers: set[str]) -> dict[str, float]:
    """Replay today's persisted spend into the provider counters (fail-soft)."""
    try:
        spent = action_repo.spend_today_by_provider(conn, action_kind=SPEND_ACTION_KIND)
    except Exception as exc:  # noqa: BLE001
        logger.warning("persona_eval: could not read today's spend ledger: %s", str(exc)[:160])
        rollback_quietly(conn)
        return {}
    for provider in providers:
        rate_limit.seed_provider_cost(provider, float(spent.get(provider, 0.0)))
    return spent


def _persist_spend(
    conn: Any,
    *,
    model_summaries: list[dict[str, Any]],
    run_id: str | None,
    objective_id: str | None,
) -> int:
    """One ledger row per model that was actually called (fail-soft)."""
    written = 0
    for ms in model_summaries:
        if int(ms.get("calls") or 0) - int(ms.get("cap_exceeded") or 0) <= 0:
            continue
        try:
            action_repo.insert_action(
                conn,
                action_kind=SPEND_ACTION_KIND,
                action_source="harness",
                model=str(ms["model"]),
                provider=str(ms["provider"]),
                cost_usd=float(ms.get("cost_usd") or 0.0),
                status="executed",
                input_payload={
                    "run_id": run_id,
                    "objective_id": objective_id,
                    "calls": ms.get("calls"),
                    "ok": ms.get("ok"),
                    "fallback": ms.get("fallback"),
                    "cap_exceeded": ms.get("cap_exceeded"),
                },
                output_payload={
                    "input_tokens": ms.get("input_tokens"),
                    "output_tokens": ms.get("output_tokens"),
                    "elapsed_ms": ms.get("elapsed_ms"),
                    "cost_usd_estimate": ms.get("cost_usd"),
                },
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("persona_eval: spend ledger write failed: %s", str(exc)[:160])
            rollback_quietly(conn)
    return written


__all__ = [
    "DEFAULT_EVAL_MODELS",
    "DISSENT",
    "PER_SYMBOL_TIMEOUT_S",
    "SPEND_ACTION_KIND",
    "consensus",
    "eval_models",
    "most_severe",
    "provider_of",
]
