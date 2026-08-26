"""Bearer token user resolution for Research API (RS-KB2)."""

from __future__ import annotations

import os
from functools import lru_cache


def _parse_users_raw(raw: str) -> dict[str, str]:
    """Map bearer token -> owner_id from ``alice:tok_xxx,bob:tok_yyy``."""
    out: dict[str, str] = {}
    for part in raw.split(","):
        piece = part.strip()
        if not piece or ":" not in piece:
            continue
        owner, token = piece.split(":", 1)
        owner = owner.strip()
        token = token.strip()
        if owner and token:
            out[token] = owner
    return out


@lru_cache(maxsize=1)
def token_to_owner_map() -> dict[str, str]:
    raw = os.environ.get("RESEARCH_USERS", "").strip()
    if not raw:
        legacy = os.environ.get("RESEARCH_API_TOKEN", "").strip()
        if legacy:
            return {legacy: os.environ.get("RESEARCH_DEFAULT_OWNER", "owner")}
        return {}
    return _parse_users_raw(raw)


def auth_required() -> bool:
    return bool(token_to_owner_map())


def resolve_owner_from_token(token: str) -> str | None:
    token = (token or "").strip()
    if not token:
        return None
    return token_to_owner_map().get(token)


def default_owner_when_auth_disabled() -> str:
    return os.environ.get("RESEARCH_DEFAULT_OWNER", "owner").strip() or "owner"


__all__ = [
    "auth_required",
    "default_owner_when_auth_disabled",
    "resolve_owner_from_token",
    "token_to_owner_map",
]
