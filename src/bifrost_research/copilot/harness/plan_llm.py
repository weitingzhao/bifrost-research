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
  E3: Default model ``deepseek-reasoner``; ``policy_json.llm_model`` may override.

All failures fall back silently by returning ``None`` so the runtime uses the
heuristic template.  D10 BLOCKED — this module never touches Trade DB or the
IB operator command stream.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

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

VALID_OPS = frozenset(
    {
        "scan_universe",
        "signal_decay_check",
        OP_ANALYZE_SYMBOL,
        OP_RUN_BACKTEST,
        OP_COMPOSE_REPORT,
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
    }
)

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MODEL = "deepseek-reasoner"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


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
        "min_composite_score, min_hit_rate, max_candidates, universe_mode, layers, option_overlay>}\n\n"
        f"Allowed op values (whitelist): {sorted(VALID_OPS)}.\n"
        "Recommended order: scan_universe → signal_decay_check → analyze_symbol → "
        "run_backtest → propose_candidates → compose_report → await_approval.\n"
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
        "policy_suggestion is advisory only; the Owner still has to update the objective policy.\n"
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


def generate_plan_llm(
    objective: dict[str, Any],
    *,
    playbook_rules: list[dict[str, Any]] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Attempt to build a plan via LLM.  Returns None on any failure (fail-soft).

    Success shape::

        {
          "steps": [{"op": str, "note": str}, ...],
          "reasoning": str | None,
          "policy_suggestion": dict | None,
          "llm_model": str,
        }
    """
    policy = objective.get("policy_json") or {}

    enabled, reason = is_llm_plan_enabled(policy)
    if not enabled:
        logger.debug("harness LLM plan disabled: %s", reason)
        return None

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        logger.info("harness LLM plan: DEEPSEEK_API_KEY not set; fallback heuristic")
        return None

    model = _resolve_model(policy)
    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    messages = _build_messages(objective, playbook_rules)

    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        logger.warning("harness LLM plan: timeout after %.1fs; fallback heuristic", timeout_seconds)
        return None
    except httpx.HTTPError as exc:
        logger.warning("harness LLM plan: httpx error %s; fallback heuristic", exc)
        return None

    if resp.status_code >= 400:
        logger.warning(
            "harness LLM plan: HTTP %s; fallback heuristic. body=%s",
            resp.status_code,
            resp.text[:200],
        )
        return None

    try:
        payload = resp.json()
    except ValueError:
        logger.warning("harness LLM plan: non-json response; fallback heuristic")
        return None

    choices = payload.get("choices") or []
    if not choices:
        logger.warning("harness LLM plan: empty choices; fallback heuristic")
        return None
    message = (choices[0] or {}).get("message") or {}
    content = str(message.get("content") or "")

    parsed = _parse_llm_json(content)
    if parsed is None:
        logger.warning("harness LLM plan: could not parse JSON; fallback heuristic")
        return None

    try:
        validated = LLMPlanResponse.model_validate(parsed)
    except ValidationError as exc:
        logger.warning("harness LLM plan: schema violation %s; fallback heuristic", exc.errors()[:1])
        return None

    return {
        "steps": [step.model_dump() for step in validated.steps],
        "reasoning": validated.reasoning,
        "policy_suggestion": validated.policy_suggestion,
        "llm_model": model,
    }
