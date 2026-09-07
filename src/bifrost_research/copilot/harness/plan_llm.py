"""LLM-driven harness plan generation — Wave Y.2 Loop Smartness.

Optional in-process LLM step that upgrades ``_plan_for_objective`` from a static
4-step template into a dynamic plan.  Decisions per spine ``D-Loop-Smartness-Y2``:

  A1: Enabled by env ``BIFROST_HARNESS_LLM_PLAN=1`` **and** not explicitly disabled
      by ``objective.policy_json.use_llm_plan=false`` (policy can also enable
      overriding the env when set to true).
  B2: LLM may add/remove/reorder steps whose ``op`` is in the whitelist, and may
      write ``plan.policy_suggestion`` (advisory; runtime never mutates the
      stored ``objective.policy_json``).
  C1: No data reflow — LLM sees only the objective + policy; scan/decay reads
      remain in the propose stage.
  E3: Default model ``deepseek-chat`` (B1 — the reasoner ignored json mode and
      blew the old 15s budget, so every DEV run fell back); ``policy_json.llm_model``
      may override. Provider chain (B1): the policy's model → ``deepseek-chat`` →
      ``gpt-4o-mini`` → heuristic template; every hop is recorded in ``attempts``.

All failures fall back by returning ``None`` (plus the attempts, via
``plan_with_chain``) so the runtime uses the heuristic template and the trace
says which model failed how.  D10 BLOCKED — this module never touches Trade DB or the
IB operator command stream.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from bifrost_research.copilot.models import ChatEndpoint, ModelConfigError, resolve_chat_endpoint
from bifrost_research.copilot.providers import estimate_cost

logger = logging.getLogger(__name__)

# The op the runtime actually branches on. Adding an op here without a branch in
# runtime.py buys nothing — the plan would describe a step that never runs.
OP_ANALYZE_SYMBOL = "analyze_symbol"
# Validate the batch against history, and turn it into something readable.
# Without these the planner could scan, focus on one symbol and propose — but
# never check whether the signal has ever worked, and never produce the report
# the Loop exists to deliver.
OP_RUN_BACKTEST = "run_backtest"
OP_COMPOSE_REPORT = "compose_report"
OP_PERSONA_EVALUATE = "persona_evaluate"

VALID_OPS = frozenset(
    {
        "scan_universe",
        "signal_decay_check",
        OP_ANALYZE_SYMBOL,
        OP_RUN_BACKTEST,
        OP_COMPOSE_REPORT,
        OP_PERSONA_EVALUATE,
        "propose_candidates",
        "await_approval",
    }
)

# Wave Y.3: whitelist of keys the LLM is allowed to suggest for policy_json.
# Must stay a subset of the policy_json fields the runtime actually honors.
POLICY_SUGGESTION_KEYS = frozenset(
    {
        "preset",
        "flag_filter",
        "min_composite_score",
        "min_hit_rate",
        "max_candidates",
        "universe_mode",
        "layers",
        "option_overlay",
        "require_validate_pass",
        "discovery_assist",
        "resolution",
        "min_source_hit_rate",
    }
)

DEFAULT_TIMEOUT_SECONDS = 60.0
# The chat model, not the reasoner: json mode works and a plan comes back in
# seconds. ``policy_json.llm_model`` may still pick something else.
DEFAULT_MODEL = "deepseek-chat"
# Second hop on another provider, so one outage cannot take both.
FALLBACK_MODEL = "gpt-4o-mini"
JSON_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}


class LLMPlanStep(BaseModel):
    op: str
    note: str = ""

    @field_validator("op")
    @classmethod
    def _op_in_whitelist(cls, value: str) -> str:
        v = str(value or "").strip()
        if v not in VALID_OPS:
            raise ValueError(f"op {v!r} not in whitelist {sorted(VALID_OPS)}")
        return v


class LLMPlanResponse(BaseModel):
    steps: list[LLMPlanStep] = Field(..., min_length=1, max_length=12)
    reasoning: str | None = None
    policy_suggestion: dict[str, Any] | None = None

    @field_validator("policy_suggestion", mode="before")
    @classmethod
    def _filter_policy_suggestion(cls, value: Any) -> Any:
        """Y.3: silently drop keys outside the whitelist so the LLM cannot
        propose fields the runtime does not honor (defense in depth — the
        Owner-approval path also filters, but doing it here keeps the trace
        clean)."""
        if value is None:
            return None
        if not isinstance(value, dict):
            # Pydantic v2 wraps ValueError → ValidationError (not TypeError).
            raise ValueError("policy_suggestion must be an object")  # noqa: TRY004
        filtered = {k: v for k, v in value.items() if k in POLICY_SUGGESTION_KEYS}
        return filtered or None


def _truthy_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_llm_plan_enabled(policy: dict[str, Any] | None) -> tuple[bool, str]:
    """Return (enabled, reason).

    Policy explicit ``use_llm_plan`` takes precedence; else env decides.
    """
    policy = policy or {}
    explicit = policy.get("use_llm_plan")
    if isinstance(explicit, bool):
        if explicit:
            return True, "policy.use_llm_plan=true"
        return False, "policy.use_llm_plan=false"
    env_on = _truthy_env(os.environ.get("BIFROST_HARNESS_LLM_PLAN"))
    if env_on:
        return True, "env BIFROST_HARNESS_LLM_PLAN"
    return False, "env off and policy unset"


def _resolve_model(policy: dict[str, Any] | None) -> str:
    policy = policy or {}
    raw = policy.get("llm_model")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return DEFAULT_MODEL


def _playbook_block(rules: list[dict[str, Any]] | None) -> str:
    """Render the Owner's trading rules for the planner to follow.

    The policy says which symbols to look at; these say how to judge them. They
    are the adjustable half of "brain + strategy" — written in the Playbook, read
    here, with no new schema and no new dependency. An empty list renders nothing
    and the planner behaves exactly as before.
    """
    if not rules:
        return ""
    lines = []
    for r in rules[:20]:
        title = str(r.get("title") or "").strip()
        body = " ".join(str(r.get("body_md") or "").split())[:300]
        if not title and not body:
            continue
        lines.append(f"- {title}: {body}" if title else f"- {body}")
    if not lines:
        return ""
    return (
        "\n\nOWNER TRADING RULES — follow these when deciding what to propose and "
        "which steps the plan needs. They come from the Owner's Playbook and "
        "outrank your own preferences; if one conflicts with the objective, say so "
        "in `reasoning` rather than silently ignoring it.\n" + "\n".join(lines)
    )


def _build_messages(
    objective: dict[str, Any],
    playbook_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Assemble system + user prompt.  Objective only — no scan/decay reflow (C1)."""
    policy = objective.get("policy_json") or {}
    system = (
        "You are the Bifrost Research Harness planner. Given a research objective, "
        "produce a JSON plan the runtime will execute.\n\n"
        "STRICT OUTPUT: Reply with a single JSON object (no markdown fences), matching this schema:\n"
        '  {"steps": [{"op": <op>, "note": <string>}, ...],\n'
        '   "reasoning": <optional short string, <=400 chars>,\n'
        '   "policy_suggestion": <optional object with keys among preset, flag_filter, '
        "min_composite_score, min_hit_rate, max_candidates, universe_mode, layers, option_overlay, "
        "resolution>}\n\n"
        f"Allowed op values (whitelist): {sorted(VALID_OPS)}.\n"
        "Recommended order: scan_universe → signal_decay_check → analyze_symbol → "
        "run_backtest → propose_candidates → persona_evaluate → compose_report → await_approval.\n"
        "persona_evaluate has every configured judge model read each candidate and keeps\n"
        "only what they agree on. It runs whenever policy_json.persona_evaluate is true\n"
        "(the default) no matter what the plan says, so list it in its place rather than\n"
        "leaving it out.\n"
        "analyze_symbol attaches per-candidate evidence: why it was selected, price\n"
        "context, option analytics where they exist, and this source's settled hit\n"
        "rate. Drop it only when the objective explicitly wants a bare list.\n"
        "run_backtest checks the batch against history before proposing it. Include\n"
        "it whenever the objective cares whether the signal has ever worked.\n"
        "compose_report turns the batch into a readable verdict: why each name was\n"
        "picked, where its price sits, how this source has actually settled, and what\n"
        "would make the call wrong. Include it when the objective asks for a report\n"
        "or a recommendation rather than a bare candidate list.\n"
        "For universe_mode stock_composite/sepa/momentum/events: describe SEPA/momentum/event layers; "
        "do NOT mention IV hot watchlist unless option_overlay.enabled is true.\n"
        "signal_decay_check applies only to scan_legacy (option scan) mode.\n"
        "You may drop / reorder steps if the objective calls for it, but you must include propose_candidates and await_approval.\n"
        "policy_suggestion is advisory only; the Owner still has to update the objective policy. "
        "Include it ONLY when you recommend changing something, and then only the keys you "
        "want changed with their new values — never echo the current policy_json back, and "
        "omit the field entirely when the policy is fine as it is.\n"
        "D10 BLOCKED — you are proposing research candidates only, never orders."
        + _playbook_block(playbook_rules)
    )
    user_body = {
        "objective": {
            "id": objective.get("id"),
            "title": objective.get("title"),
            "description": objective.get("description"),
            "persona": objective.get("persona"),
            "schedule": objective.get("schedule"),
        },
        "policy_json": policy,
    }
    user = "OBJECTIVE:\n" + json.dumps(user_body, indent=2, default=str)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    r"""Extract JSON object from an LLM response.

    Tolerates leading/trailing ``\`\`\`json`` fences and free-text preamble.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    # If preamble noise: pick the outermost {...}
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def model_chain(policy: dict[str, Any] | None) -> list[str]:
    """Models to try in order: the policy's pick (or the default), then the
    remaining default hops. Distinct providers, so one outage cannot take both."""
    chain = [_resolve_model(policy)]
    for model in (DEFAULT_MODEL, FALLBACK_MODEL):
        if model not in chain:
            chain.append(model)
    return chain


def _supports_json_mode(model: str) -> bool:
    # DeepSeek's reasoner rejects ``response_format``; everything else in the
    # chain honours it.
    return "reasoner" not in model.lower()


def _new_attempt(model: str, provider: str) -> dict[str, Any]:
    return {
        "model": model,
        "provider": provider,
        "ok": False,
        "elapsed_ms": 0,
        "error": None,
        "http_status": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }


def _call_endpoint(
    endpoint: ChatEndpoint,
    messages: list[dict[str, str]],
    timeout_seconds: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """One hop of the chain: call, parse, validate. Never raises.

    Returns ``(validated plan fields | None, attempt record)``. The record is
    what the trace shows, so every exit path names its failure.
    """
    attempt = _new_attempt(endpoint.model, endpoint.provider)
    api_key = endpoint.api_key
    if not api_key:
        attempt["error"] = f"{endpoint.api_key_env} not configured"
        return None, attempt

    body: dict[str, Any] = {
        "model": endpoint.model,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    if _supports_json_mode(endpoint.model):
        body["response_format"] = dict(JSON_RESPONSE_FORMAT)

    started = time.perf_counter()

    def fail(error: str, status: int | None = None) -> tuple[None, dict[str, Any]]:
        attempt["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        attempt["error"] = error
        attempt["http_status"] = status
        return None, attempt

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(
                f"{endpoint.base_url}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        return fail(f"timeout after {timeout_seconds:.0f}s")
    except httpx.HTTPError as exc:
        return fail(f"httpx error {exc}")

    if resp.status_code >= 400:
        logger.warning(
            "harness LLM plan: %s@%s HTTP %s body=%s",
            endpoint.model,
            endpoint.provider,
            resp.status_code,
            resp.text[:200],
        )
        return fail(f"HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except ValueError:
        return fail("non-json response", resp.status_code)

    usage = payload.get("usage") or {}
    attempt["input_tokens"] = int(usage.get("prompt_tokens") or 0)
    attempt["output_tokens"] = int(usage.get("completion_tokens") or 0)
    attempt["cost_usd"] = round(
        estimate_cost(endpoint.model, attempt["input_tokens"], attempt["output_tokens"]), 6
    )

    choices = payload.get("choices") or []
    if not choices:
        return fail("empty choices", resp.status_code)
    message = (choices[0] or {}).get("message") or {}
    content = str(message.get("content") or "")

    parsed = _parse_llm_json(content)
    if parsed is None:
        return fail("unparseable content", resp.status_code)

    try:
        validated = LLMPlanResponse.model_validate(parsed)
    except ValidationError as exc:
        first = exc.errors()[:1]
        detail = first[0].get("msg") if first else "invalid"
        return fail(f"schema violation: {detail}", resp.status_code)

    attempt["ok"] = True
    attempt["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    attempt["http_status"] = resp.status_code
    return (
        {
            "steps": [step.model_dump() for step in validated.steps],
            "reasoning": validated.reasoning,
            "policy_suggestion": validated.policy_suggestion,
        },
        attempt,
    )


def plan_with_chain(
    objective: dict[str, Any],
    *,
    playbook_rules: list[dict[str, Any]] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    models: Sequence[str] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Walk the model chain until one hop returns a valid plan.

    Returns ``(plan | None, attempts)``. Success shape::

        {
          "steps": [{"op": str, "note": str}, ...],
          "reasoning": str | None,
          "policy_suggestion": dict | None,
          "llm_model": str,
          "llm_provider": str,
          "attempts": [{"model", "provider", "ok", "elapsed_ms", "error", ...}],
        }

    ``attempts`` is returned alongside on failure too, so the caller can say
    which model failed how instead of a bare "call failed".
    """
    policy = objective.get("policy_json") or {}

    enabled, reason = is_llm_plan_enabled(policy)
    if not enabled:
        logger.debug("harness LLM plan disabled: %s", reason)
        return None, []

    messages = _build_messages(objective, playbook_rules)
    attempts: list[dict[str, Any]] = []
    for model in list(models) if models else model_chain(policy):
        try:
            endpoint = resolve_chat_endpoint(model)
        except ModelConfigError as exc:
            attempt = _new_attempt(model, "unknown")
            attempt["error"] = str(exc)
            attempts.append(attempt)
            continue
        result, attempt = _call_endpoint(endpoint, messages, timeout_seconds)
        attempts.append(attempt)
        if result is not None:
            logger.info(
                "harness LLM plan: %s@%s in %dms",
                endpoint.model,
                endpoint.provider,
                attempt["elapsed_ms"],
            )
            return (
                {
                    **result,
                    "llm_model": endpoint.model,
                    "llm_provider": endpoint.provider,
                    "attempts": attempts,
                },
                attempts,
            )
        logger.warning(
            "harness LLM plan: %s@%s failed: %s",
            endpoint.model,
            endpoint.provider,
            attempt["error"],
        )
    return None, attempts


def failure_reason(attempts: list[dict[str, Any]]) -> str:
    """The fallback reason the trace shows: every hop, named, in order."""
    if not attempts:
        return "llm_failed: no model attempted"
    parts = [
        f"{a.get('model')}@{a.get('provider')} {a.get('error') or 'failed'}" for a in attempts
    ]
    return "llm_failed: " + "; ".join(parts)


def generate_plan_llm(
    objective: dict[str, Any],
    *,
    playbook_rules: list[dict[str, Any]] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Plan via the model chain; ``None`` when every hop failed (fail-soft)."""
    return plan_with_chain(
        objective, playbook_rules=playbook_rules, timeout_seconds=timeout_seconds
    )[0]
