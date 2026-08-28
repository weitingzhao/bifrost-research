"""Unit tests for ephemeral Copilot client_context injection."""

from __future__ import annotations

from bifrost_research.api.copilot import ClientContext
from bifrost_research.copilot.client_context import (
    SNAPSHOT_MAX_BYTES,
    bound_snapshot,
    client_context_is_empty,
    format_client_context_system_message,
    inject_client_context_message,
)


def test_empty_context_is_noop() -> None:
    assert client_context_is_empty(None) is True
    assert client_context_is_empty({}) is True
    assert client_context_is_empty(ClientContext().model_dump()) is True
    assert format_client_context_system_message(None) is None
    assert format_client_context_system_message({}) is None
    msgs = [{"role": "user", "content": "hi"}]
    assert inject_client_context_message(msgs, None) is msgs
    assert inject_client_context_message(msgs, {}) is msgs


def test_format_full_client_context() -> None:
    ctx = ClientContext(
        origin_page="vrp-lab",
        origin_label="VRP Lab",
        symbol="AAPL",
        date="2026-08-27",
        panel="term-structure",
        snapshot={"vrp": 0.12, "iv": 0.33},
        suggested_prompt="Explain AAPL VRP",
    )
    text = format_client_context_system_message(ctx.model_dump())
    assert text == (
        "Client view context: origin=VRP Lab (vrp-lab); symbol=AAPL; "
        'date=2026-08-27; panel=term-structure; snapshot={"vrp":0.12,"iv":0.33}; '
        "suggested_prompt=Explain AAPL VRP"
    )


def test_snapshot_drops_secret_keys() -> None:
    cleaned = bound_snapshot(
        {
            "vrp": 0.1,
            "api_key": "sk-live-secret",
            "token": "abc",
            "password": "hunter2",
            "authorization": "Bearer xyz",
        }
    )
    assert cleaned == {"vrp": 0.1}
    text = format_client_context_system_message(
        {
            "symbol": "AAPL",
            "snapshot": {"vrp": 0.1, "api_key": "sk-live-secret", "access_token": "t"},
        }
    )
    assert text is not None
    assert "sk-live-secret" not in text
    assert "access_token" not in text
    assert '"vrp":0.1' in text


def test_snapshot_drops_oversized_fields() -> None:
    huge = "x" * (SNAPSHOT_MAX_BYTES + 200)
    cleaned = bound_snapshot({"keep": 1, "blob": huge})
    assert cleaned == {"keep": 1}
    text = format_client_context_system_message(
        {"symbol": "MSFT", "snapshot": {"keep": 1, "blob": huge}}
    )
    assert text is not None
    assert huge not in text
    assert "symbol=MSFT" in text


def test_inject_prepends_system_and_leaves_user_intact() -> None:
    msgs = [{"role": "user", "content": "What is VRP?"}]
    out = inject_client_context_message(msgs, {"origin_label": "VRP Lab", "symbol": "AAPL"})
    assert out is not msgs
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "Client view context: origin=VRP Lab; symbol=AAPL"
    assert out[1] == {"role": "user", "content": "What is VRP?"}
    assert msgs == [{"role": "user", "content": "What is VRP?"}]
