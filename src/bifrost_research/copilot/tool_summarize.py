"""Tool result compression for Context Bridge (mirrors FE toolMeta.summarize — D7)."""

from __future__ import annotations

import json
from typing import Any


def _as_record(v: Any) -> dict[str, Any] | None:
    if v and isinstance(v, dict):
        return v
    return None


def _as_array(v: Any) -> list[Any] | None:
    return v if isinstance(v, list) else None


def unwrap_envelope(envelope: Any) -> tuple[bool, Any, str | None]:
    env = _as_record(envelope)
    if not env:
        return False, envelope, None
    if "data" in env:
        ok = env.get("ok") is not False
        err = env.get("error") if isinstance(env.get("error"), str) else None
        return ok, env.get("data"), err
    return True, envelope, None


def generic_summary(data: Any) -> list[str]:
    d = _as_record(data)
    if not d:
        return []
    lines: list[str] = []
    for key in (
        "items",
        "rows",
        "executions",
        "instances",
        "opportunities",
        "quotes",
        "candidates",
        "rules",
        "cases",
        "sessions",
    ):
        arr = _as_array(d.get(key))
        if arr is not None:
            lines.append(f"{len(arr)} {key}")
            sample = arr[0] if arr else None
            if isinstance(sample, dict):
                for k in list(sample.keys())[:4]:
                    val = sample.get(k)
                    if val is not None and not isinstance(val, (dict, list)):
                        lines.append(f"  sample.{k}: {str(val)[:80]}")
            break
    if not lines:
        for k, v in list(d.items())[:6]:
            if v is None or isinstance(v, (dict, list)):
                continue
            lines.append(f"{k}: {str(v)[:80]}")
    return lines


def summarize_tool_result(tool_name: str, envelope: Any) -> str:
    ok, data, error = unwrap_envelope(envelope)
    parts = [f"tool={tool_name}", f"ok={ok}"]
    if error:
        parts.append(f"error={error}")
    if ok:
        parts.extend(generic_summary(data))
    return " | ".join(parts)


def frame_to_text(frame: dict[str, Any]) -> str | None:
    kind = frame.get("kind") or (
        "text" if frame.get("role") in ("user", "assistant") else frame.get("role")
    )
    if kind == "handoff":
        return f"[handoff {frame.get('agent_from')} → {frame.get('agent_to')}]"
    if kind == "tool_call":
        args = frame.get("args") or {}
        return f"[tool_call {frame.get('tool_name')} args={json.dumps(args, ensure_ascii=False)[:200]}]"
    if kind == "tool_result":
        name = str(frame.get("tool_name") or "unknown")
        if frame.get("ok"):
            payload = {"ok": True, "data": frame.get("data")}
        else:
            payload = {"ok": False, "error": frame.get("error")}
        return summarize_tool_result(name, payload)
    role = frame.get("role")
    content = str(frame.get("content") or "").strip()
    if not content:
        return None
    if role == "user":
        return f"User: {content}"
    if role == "assistant":
        return f"Assistant: {content}"
    return content


def frames_to_context(frames: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for frame in frames:
        line = frame_to_text(frame)
        if line:
            lines.append(line)
    return "\n".join(lines)


__all__ = ["frames_to_context", "summarize_tool_result"]
