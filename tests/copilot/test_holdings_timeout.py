"""The optional holdings overlay must not dominate the run.

Stage timing showed persona_evaluate taking 5.02s of a 5.12s run — 98% — and all
of it was one call to an unreachable Trade monitor, timing out on the default 8s
budget before the portfolio persona abstained anyway. The overlay is best-effort
by construction; nothing downstream needs it.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import persona_eval


def test_the_snapshot_call_gets_a_short_budget(monkeypatch: Any) -> None:
    persona_eval.reset_holdings_probe_cache()
    seen: dict[str, Any] = {}

    def _get(base: str, path: str, params: Any = None, *, timeout: float | None = None) -> Any:
        seen["timeout"] = timeout
        raise RuntimeError("unreachable")

    monkeypatch.setattr("bifrost_research.mcp.tools._trade_api_client.get", _get)
    held, status = persona_eval.load_held_symbols()

    assert status == "unavailable"
    assert held is None
    assert seen["timeout"] == persona_eval.HOLDINGS_SNAPSHOT_TIMEOUT_S
    # A budget anywhere near the 8s default puts us back where we started.
    assert seen["timeout"] <= 2.0


def test_an_unreachable_monitor_still_fails_soft(monkeypatch: Any) -> None:
    persona_eval.reset_holdings_probe_cache()

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("connect timeout")

    monkeypatch.setattr("bifrost_research.mcp.tools._trade_api_client.get", _boom)
    assert persona_eval.load_held_symbols() == (None, "unavailable")


def test_a_reachable_monitor_is_still_used(monkeypatch: Any) -> None:
    persona_eval.reset_holdings_probe_cache()
    # Shortening the budget must not turn a working snapshot into "unavailable".
    def _ok(*a: Any, **k: Any) -> Any:
        # Shape as /status returns it — _extract_light_status reads
        # portfolio.accounts, not a bare accounts key.
        return {"portfolio": {"accounts": [{"positions": [{"symbol": "AAPL"}]}]}}

    monkeypatch.setattr("bifrost_research.mcp.tools._trade_api_client.get", _ok)
    held, status = persona_eval.load_held_symbols()
    assert status == "applied"
    assert held == {"AAPL"}


def test_a_failed_probe_is_remembered(monkeypatch: Any) -> None:
    """The real cost was DNS, which no HTTP timeout bounds.

    `api-monitor.bifrost-prod.svc.cluster.local` resolves inside the cluster and
    not on a dev machine, where `getaddrinfo` blocks ~5s — measured at 5.02s even
    with a 0.2s HTTP budget. Paying that once per run to re-learn the same answer
    was 98% of a local run.
    """
    persona_eval.reset_holdings_probe_cache()
    calls = {"n": 0}

    def _boom(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        raise RuntimeError("nodename nor servname provided")

    monkeypatch.setattr("bifrost_research.mcp.tools._trade_api_client.get", _boom)
    assert persona_eval.load_held_symbols() == (None, "unavailable")
    assert persona_eval.load_held_symbols() == (None, "unavailable")
    assert persona_eval.load_held_symbols() == (None, "unavailable")
    assert calls["n"] == 1, "a known-unreachable monitor was probed again"


def test_the_memory_expires(monkeypatch: Any) -> None:
    persona_eval.reset_holdings_probe_cache()
    monkeypatch.setattr(persona_eval, "HOLDINGS_UNAVAILABLE_TTL_S", 0.0)
    calls = {"n": 0}

    def _boom(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        raise RuntimeError("unreachable")

    monkeypatch.setattr("bifrost_research.mcp.tools._trade_api_client.get", _boom)
    persona_eval.load_held_symbols()
    persona_eval.load_held_symbols()
    assert calls["n"] == 2, "a monitor that came back must be found again"


def test_a_working_snapshot_is_never_cached(monkeypatch: Any) -> None:
    """Holdings move. A remembered 'applied' would overlay a stale book."""
    persona_eval.reset_holdings_probe_cache()
    calls = {"n": 0}

    def _ok(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return {"portfolio": {"accounts": [{"positions": [{"symbol": "AAPL"}]}]}}

    monkeypatch.setattr("bifrost_research.mcp.tools._trade_api_client.get", _ok)
    persona_eval.load_held_symbols()
    persona_eval.load_held_symbols()
    assert calls["n"] == 2
