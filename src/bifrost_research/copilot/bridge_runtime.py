"""Context Bridge runtime — compress session + LLM polish (Wave RS-EX2)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Protocol

from bifrost_research.copilot.bridge_presets import (
    DEFAULT_BRIDGE_MODEL,
    FOCUS_HINTS,
    TARGET_LABELS,
    validate_depth,
    validate_focus,
    validate_target,
)
from bifrost_research.copilot.bridge_rate_limit import check_bridge_rate_limit
from bifrost_research.copilot.providers import ProviderTurn, estimate_cost
from bifrost_research.copilot.tool_summarize import frames_to_context
from bifrost_research.db.conn import connect
from bifrost_research.repositories import copilot_bridge as bridge_repo
from bifrost_research.repositories import copilot_session as session_repo

logger = logging.getLogger("bifrost.copilot.bridge")

_INSTRUCTIONS = (
    Path(__file__).resolve().parent / "agents" / "instructions" / "bridge.md"
)


class _ChatProvider(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
    ) -> ProviderTurn: ...


def _read_bridge_instructions() -> str:
    if _INSTRUCTIONS.is_file():
        return _INSTRUCTIONS.read_text(encoding="utf-8").strip()
    return "Polish the context into markdown for an external AI assistant."


def _resolve_chat_provider(model: str) -> _ChatProvider:
    lower = (model or "").lower()
    if lower.startswith("deepseek"):
        return _DeepSeekChatProvider()
    if lower.startswith("gpt") or lower.startswith("openai"):
        return _OpenAIChatProvider()
    if lower.startswith("claude"):
        return _ClaudeChatProvider()
    return _DeepSeekChatProvider()


class _DeepSeekChatProvider:
    def complete(self, *, messages: list[dict[str, Any]], model: str) -> ProviderTurn:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            return ProviderTurn(error="DEEPSEEK_API_KEY not configured")
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError:
            return ProviderTurn(error="openai package not installed")
        base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        client = OpenAI(base_url=base, api_key=api_key)
        mid = model if model.startswith("deepseek") else "deepseek-chat"
        try:
            resp = client.chat.completions.create(
                model=mid,
                messages=messages,
                temperature=0.3,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderTurn(error=f"DeepSeek API error: {exc}")
        choice = resp.choices[0].message
        text = choice.content or ""
        usage = resp.usage
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
        return ProviderTurn(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_cost(mid, in_tok, out_tok),
        )


class _OpenAIChatProvider:
    def complete(self, *, messages: list[dict[str, Any]], model: str) -> ProviderTurn:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return ProviderTurn(error="OPENAI_API_KEY not configured")
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError:
            return ProviderTurn(error="openai package not installed")
        client = OpenAI(api_key=api_key)
        mid = model if model.startswith("gpt") else "gpt-4o-mini"
        try:
            resp = client.chat.completions.create(model=mid, messages=messages, temperature=0.3)
        except Exception as exc:  # noqa: BLE001
            return ProviderTurn(error=f"OpenAI API error: {exc}")
        choice = resp.choices[0].message
        usage = resp.usage
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
        return ProviderTurn(
            text=choice.content or "",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_cost(mid, in_tok, out_tok),
        )


class _ClaudeChatProvider:
    def complete(self, *, messages: list[dict[str, Any]], model: str) -> ProviderTurn:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return ProviderTurn(error="ANTHROPIC_API_KEY not configured")
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError:
            return ProviderTurn(error="anthropic package not installed")
        client = anthropic.Anthropic(api_key=api_key)
        system = ""
        api_messages: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system = str(msg.get("content", ""))
            else:
                api_messages.append(msg)
        mid = model if model.startswith("claude") else "claude-sonnet-4-20250514"
        try:
            resp = client.messages.create(
                model=mid,
                max_tokens=4096,
                system=system,
                messages=api_messages,
                temperature=0.3,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderTurn(error=f"Anthropic API error: {exc}")
        text = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text += block.text
        in_tok = int(getattr(resp.usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(resp.usage, "output_tokens", 0) or 0)
        return ProviderTurn(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_cost(mid, in_tok, out_tok),
        )


def _filter_frames_from_message(
    frames: list[dict[str, Any]],
    frames_from_message_id: str | None,
) -> list[dict[str, Any]]:
    if not frames_from_message_id:
        return frames
    # Match persisted frame index or synthetic UI id suffix.
    marker = frames_from_message_id
    start_idx = 0
    for i, frame in enumerate(frames):
        fid = frame.get("id") or frame.get("message_id")
        if fid and str(fid) == marker:
            start_idx = i
            break
        # UI ids like hist-{session}-{seq} — match trailing seq against index
        if marker.startswith("hist-") and marker.endswith(f"-{i}"):
            start_idx = i
            break
    return frames[start_idx:]


def _fallback_markdown(
    *,
    focus: str,
    depth: str,
    target: str,
    context: str,
    session_title: str | None,
) -> str:
    title = session_title or "Copilot session"
    lines = [
        "# Context for external AI",
        "",
        f"**Source:** Bifrost Research Copilot — {title}",
        f"**Focus:** {focus} · **Depth:** {depth} · **Target:** {TARGET_LABELS.get(target, target)}",
        "",
        "## Conversation summary",
        "",
        context or "_No context frames._",
        "",
        "## Suggested follow-ups",
        "",
        "- Ask the external assistant to stress-test the thesis above.",
        "- Request a checklist of risks not yet covered.",
    ]
    if depth == "deep":
        lines.extend(
            [
                "- Compare this setup to your playbook rules and flag gaps.",
            ]
        )
    return "\n".join(lines)


def build_bridge(
    *,
    session_id: str,
    owner_id: str,
    focus: str,
    depth: str,
    target: str,
    model: str | None = None,
    frames_from_message_id: str | None = None,
    provider: _ChatProvider | None = None,
) -> dict[str, Any]:
    """Build bridge markdown, persist audit event, return payload."""
    allowed, retry_after = check_bridge_rate_limit(owner_id)
    if not allowed:
        return {
            "ok": False,
            "error": "bridge_rate_limit",
            "retry_after_sec": retry_after,
            "limit_per_minute": 6,
        }

    focus_v = validate_focus(focus)
    depth_v = validate_depth(depth)
    target_v = validate_target(target)
    model_id = (model or DEFAULT_BRIDGE_MODEL).strip() or DEFAULT_BRIDGE_MODEL

    conn = connect()
    try:
        row = session_repo.get_session(conn, session_id, owner_id=owner_id)
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "session_not_found"}

    frames = list(row.get("messages") or [])
    frames = _filter_frames_from_message(frames, frames_from_message_id)
    context = frames_to_context(frames)
    if not context.strip():
        return {"ok": False, "error": "empty_context"}

    user_prompt = (
        f"Focus: {focus_v} — {FOCUS_HINTS.get(focus_v, '')}\n"
        f"Depth: {depth_v}\n"
        f"Target assistant: {target_v} ({TARGET_LABELS.get(target_v, target_v)})\n\n"
        f"--- Raw compressed context ---\n{context}\n--- End context ---"
    )

    chat = provider or _resolve_chat_provider(model_id)
    turn = chat.complete(
        messages=[
            {"role": "system", "content": _read_bridge_instructions()},
            {"role": "user", "content": user_prompt},
        ],
        model=model_id,
    )

    if turn.error:
        markdown = _fallback_markdown(
            focus=focus_v,
            depth=depth_v,
            target=target_v,
            context=context,
            session_title=row.get("title"),
        )
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        polished = False
        logger.warning("bridge LLM polish failed: %s — using deterministic fallback", turn.error)
    else:
        markdown = (turn.text or "").strip() or _fallback_markdown(
            focus=focus_v,
            depth=depth_v,
            target=target_v,
            context=context,
            session_title=row.get("title"),
        )
        input_tokens = turn.input_tokens
        output_tokens = turn.output_tokens
        cost_usd = turn.cost_usd
        polished = True

    conn = connect()
    try:
        event = bridge_repo.insert_event(
            conn,
            owner_id=owner_id,
            session_id=session_id,
            focus=focus_v,
            depth=depth_v,
            target=target_v,
            model=model_id,
            frames_from_message_id=frames_from_message_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            preview_md=markdown,
            polished=polished,
        )
        session_repo.append_message(
            conn,
            session_id,
            {
                "kind": "bridge",
                "role": "system",
                "content": f"Context bridge ({focus_v}/{depth_v}/{target_v})",
                "bridge_event_id": event["id"],
                "ts": event.get("created_at"),
            },
        )
    finally:
        conn.close()

    from bifrost_research.copilot.rate_limit import record_usage

    record_usage(tokens=input_tokens + output_tokens, cost_usd=cost_usd)

    return {
        "ok": True,
        "data": {
            "markdown": markdown,
            "event_id": event["id"],
            "session_id": session_id,
            "focus": focus_v,
            "depth": depth_v,
            "target": target_v,
            "model": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "polished": polished,
        },
    }


__all__ = ["build_bridge"]
