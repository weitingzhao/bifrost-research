"""Bearer auth resolution tests — RS-KB2."""

from __future__ import annotations

import os

from bifrost_research.auth.bearer import (
    auth_required,
    resolve_owner_from_token,
    token_to_owner_map,
)


def test_resolve_owner_from_env(monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_USERS", "alice:tok_a,bob:tok_b")
    token_to_owner_map.cache_clear()
    assert resolve_owner_from_token("tok_a") == "alice"
    assert resolve_owner_from_token("tok_b") == "bob"
    assert resolve_owner_from_token("bad") is None


def test_auth_not_required_when_empty(monkeypatch) -> None:
    monkeypatch.delenv("RESEARCH_USERS", raising=False)
    monkeypatch.delenv("RESEARCH_API_TOKEN", raising=False)
    token_to_owner_map.cache_clear()
    assert auth_required() is False
