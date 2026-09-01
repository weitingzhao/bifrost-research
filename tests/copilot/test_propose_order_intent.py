"""research.loop.propose_order_intent MCP tool — Wave LO-2."""

from __future__ import annotations

from bifrost_research.copilot.harness.order_intent_schema import OrderIntent


def test_order_intent_payload_advisory():
    intent = OrderIntent(
        hypothesis_id="hyp-1",
        strategy_template="long_atm_straddle",
        rationale="test",
    )
    payload = intent.to_payload()
    assert payload["advisory"] is True
    assert payload["d10"] == "BLOCKED"
    assert payload["hypothesis_id"] == "hyp-1"
