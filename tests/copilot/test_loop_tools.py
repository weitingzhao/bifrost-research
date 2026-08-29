"""Wave C — Research Loop write tools + curator persona (offline)."""

from __future__ import annotations

from bifrost_research.copilot.agents.loop_curator import loop_curator_appendix
from bifrost_research.copilot.harness.order_intent_schema import LegSpec, OrderIntent
from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES

_LOOP_TOOLS = (
    "research.loop.propose_candidate",
    "research.loop.promote_to_hypothesis",
    "research.loop.attach_backtest_evidence",
    "research.loop.draft_decision",
)


def test_write_tool_names_include_loop_four() -> None:
    for name in _LOOP_TOOLS:
        assert name in WRITE_TOOL_NAMES


def test_order_intent_schema_validates_sample() -> None:
    intent = OrderIntent(
        hypothesis_id="hyp-demo",
        strategy_template="short_strangle_30d",
        legs=[
            LegSpec(symbol="AAPL", right="C", strike=200.0, expiry="2026-09-18", side="sell"),
            LegSpec(symbol="AAPL", right="P", strike=180.0, expiry="2026-09-18", side="sell"),
        ],
        rationale="High IV rank advisory draft",
    )
    assert intent.hypothesis_id == "hyp-demo"
    assert len(intent.legs) == 2
    assert intent.strategy_template == "short_strangle_30d"


def test_loop_curator_appendix_nonempty() -> None:
    text = loop_curator_appendix()
    assert isinstance(text, str)
    assert len(text.strip()) > 50
    assert "research.loop.propose_candidate" in text
    assert "D10" in text
