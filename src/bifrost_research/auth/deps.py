"""FastAPI dependencies for Research user identity (RS-KB2)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from bifrost_research.auth.bearer import (
    auth_required,
    default_owner_when_auth_disabled,
    resolve_owner_from_token,
)


def _extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


async def require_owner(request: Request) -> str:
    token = _extract_bearer_token(request)
    if token:
        owner = resolve_owner_from_token(token)
        if owner:
            request.state.owner_id = owner
            return owner
        if auth_required():
            raise HTTPException(status_code=401, detail="invalid research token")

    if auth_required():
        raise HTTPException(status_code=401, detail="research authorization required")

    owner = default_owner_when_auth_disabled()
    request.state.owner_id = owner
    return owner


__all__ = ["require_owner"]
