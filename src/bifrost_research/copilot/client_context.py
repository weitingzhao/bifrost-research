"""Ephemeral client-view context for Copilot stream.

Injected as a system message for the current request only. Never written into
persisted user-turn content or used as a session-restore hash input.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

SNAPSHOT_MAX_BYTES = 4096

# Drop keys that look like credentials (exact or suffix), not KPI names like token_count.
_SECRET_KEY_RE = re.compile(
    r"(^|[_-])(api[_-]?key|token|password|secret|authorization|bearer|"
    r"private[_-]?key|access[_-]?key|credential|cookie)s?$",
    re.IGNORECASE,
)

_CONTEXT_KEYS = (
    "origin_page",
    "origin_label",
    "symbol",
    "date",
    "panel",
    "snapshot",
    "suggested_prompt",
)


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, dict) and not value:
        return True
    return False


def client_context_is_empty(ctx: Mapping[str, Any] | None) -> bool:
    if ctx is None:
        return True
    return all(_is_blank(ctx.get(key)) for key in _CONTEXT_KEYS)


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, default=str, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _redact_snapshot_value(key: str, value: Any) -> Any | None:
    """Return a sanitized value, or None to drop the field."""
    if _SECRET_KEY_RE.search(str(key)):
        return None
    if isinstance(value, dict):
        cleaned = _sanitize_snapshot_dict(value)
        return cleaned if cleaned else None
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, dict):
                nested = _sanitize_snapshot_dict(item)
                if nested:
                    items.append(nested)
            elif not isinstance(item, str) or not _SECRET_KEY_RE.search(item):
                items.append(item)
        return items
    return value


def _sanitize_snapshot_dict(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, raw_val in snapshot.items():
        key = str(raw_key)
        cleaned = _redact_snapshot_value(key, raw_val)
        if cleaned is None:
            continue
        out[key] = cleaned
    return out


def bound_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Redact secrets and drop oversized fields so JSON stays ≤ SNAPSHOT_MAX_BYTES."""
    if not snapshot:
        return None
    redacted = _sanitize_snapshot_dict(snapshot)
    if not redacted:
        return None
    if len(_json_bytes(redacted)) <= SNAPSHOT_MAX_BYTES:
        return redacted

    ranked = sorted(
        redacted.items(),
        key=lambda kv: len(_json_bytes({kv[0]: kv[1]})),
        reverse=True,
    )
    kept = dict(redacted)
    for key, _value in ranked:
        if len(_json_bytes(kept)) <= SNAPSHOT_MAX_BYTES:
            break
        kept.pop(key, None)
    if not kept or len(_json_bytes(kept)) > SNAPSHOT_MAX_BYTES:
        return None
    return kept


def format_client_context_system_message(ctx: Mapping[str, Any] | None) -> str | None:
    """Stable English system prompt. Returns None when context is empty."""
    if client_context_is_empty(ctx) or ctx is None:
        return None

    parts: list[str] = []
    origin_label = str(ctx.get("origin_label") or "").strip() or None
    origin_page = str(ctx.get("origin_page") or "").strip() or None
    if origin_label and origin_page:
        parts.append(f"origin={origin_label} ({origin_page})")
    elif origin_label:
        parts.append(f"origin={origin_label}")
    elif origin_page:
        parts.append(f"origin={origin_page}")

    for key in ("symbol", "date", "panel"):
        raw = ctx.get(key)
        if isinstance(raw, str) and raw.strip():
            parts.append(f"{key}={raw.strip()}")

    raw_snapshot = ctx.get("snapshot")
    snapshot = bound_snapshot(raw_snapshot if isinstance(raw_snapshot, dict) else None)
    if snapshot is not None:
        snap_json = json.dumps(snapshot, default=str, separators=(",", ":"), ensure_ascii=False)
        parts.append(f"snapshot={snap_json}")

    suggested = ctx.get("suggested_prompt")
    if isinstance(suggested, str) and suggested.strip():
        parts.append(f"suggested_prompt={suggested.strip()}")

    if not parts:
        return None
    return "Client view context: " + "; ".join(parts)


def inject_client_context_message(
    messages: list[dict[str, Any]],
    ctx: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Prepend ephemeral system context. Does not mutate ``messages`` when injecting.

    When context is empty, returns the same list object (identical call path).
    """
    text = format_client_context_system_message(ctx)
    if not text:
        return messages
    return [{"role": "system", "content": text}, *messages]


__all__ = [
    "SNAPSHOT_MAX_BYTES",
    "bound_snapshot",
    "client_context_is_empty",
    "format_client_context_system_message",
    "inject_client_context_message",
]
