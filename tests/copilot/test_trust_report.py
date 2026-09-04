"""The Loop could not earn autonomy because nothing recorded that it ran.

`trust_gate` read the matrix; nothing wrote to it. Across 694 recorded jobs the
`research.loop.batch` scope had zero entries, so the counter sat at 0 through
weeks of green runs — the gate was not strict, it was disconnected.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import trust_gate, trust_report


def test_a_run_is_reported_with_its_outcome(monkeypatch: Any) -> None:
    monkeypatch.setenv("PLATFORM_OPERATOR_TOKEN", "t0ken")
    monkeypatch.setenv("PLATFORM_API_URL", "http://platform.test")
    sent: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

    def _post(url: str, **kw: Any) -> Any:
        sent["url"] = url
        sent["json"] = kw.get("json")
        sent["auth"] = (kw.get("headers") or {}).get("Authorization")
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", _post)
    assert trust_report.report_batch_outcome(ok=True, summary="2 objective(s)") is True
    assert sent["url"] == "http://platform.test/api/v1/agent/governance/skill-runs"
    assert sent["json"]["scope"] == trust_gate.SKILL_ID
    assert sent["json"]["status"] == "done"
    assert sent["auth"] == "Bearer t0ken"


def test_a_failure_is_reported_too(monkeypatch: Any) -> None:
    # A matrix that only hears about successes can never demote.
    monkeypatch.setenv("PLATFORM_OPERATOR_TOKEN", "t0ken")
    seen: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: (seen.update(kw["json"]), _Resp())[1])
    trust_report.report_batch_outcome(ok=False, summary="boom")
    assert seen["status"] == "failed"


def test_without_a_token_it_skips_quietly(monkeypatch: Any) -> None:
    monkeypatch.delenv("PLATFORM_OPERATOR_TOKEN", raising=False)
    import httpx

    def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("posted without a token")

    monkeypatch.setattr(httpx, "post", _boom)
    assert trust_report.report_batch_outcome(ok=True) is False


def test_an_unreachable_platform_never_fails_the_run(monkeypatch: Any) -> None:
    # The work is done and stands on its own; the control plane being down must
    # not turn a good run into a failed one.
    monkeypatch.setenv("PLATFORM_OPERATOR_TOKEN", "t0ken")
    import httpx

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx, "post", _boom)
    assert trust_report.report_batch_outcome(ok=True) is False


def test_the_gate_reads_the_field_the_matrix_actually_sends(monkeypatch: Any) -> None:
    """It looked for `effective_level` / `level`; the matrix sends `current_level`.

    So the read produced "" and the gate stayed shut no matter what was granted.
    """
    monkeypatch.setenv("BIFROST_LOOP_BATCH_MODE", "1")
    monkeypatch.delenv("BIFROST_LOOP_TRUST_L0_OVERRIDE", raising=False)

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> Any:
            return {"entries": [{"skill_id": trust_gate.SKILL_ID, "current_level": "L0"}]}

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    assert trust_gate.trust_l0_research_loop_batch() is True


def test_the_gate_stays_shut_at_l1(monkeypatch: Any) -> None:
    monkeypatch.setenv("BIFROST_LOOP_BATCH_MODE", "1")
    monkeypatch.delenv("BIFROST_LOOP_TRUST_L0_OVERRIDE", raising=False)

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> Any:
            return {"entries": [{"skill_id": trust_gate.SKILL_ID, "current_level": "L1"}]}

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    assert trust_gate.trust_l0_research_loop_batch() is False
