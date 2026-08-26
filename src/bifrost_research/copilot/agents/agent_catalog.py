"""Canonical Copilot agent names and persona metadata (Wave RS-PS)."""

from __future__ import annotations

from typing import Literal

AgentName = Literal[
    "discovery",
    "analyze",
    "validate",
    "write",
    "explain",
    "portfolio",
    "verdict",
    "curator",
]

AGENT_NAMES: tuple[str, ...] = (
    "discovery",
    "analyze",
    "validate",
    "write",
    "explain",
    "portfolio",
    "verdict",
    "curator",
)

AGENT_LABELS: dict[str, str] = {
    "discovery": "Discovery",
    "analyze": "Analyze",
    "validate": "Validate",
    "write": "Write",
    "explain": "Explain",
    "portfolio": "Portfolio",
    "verdict": "Verdict",
    "curator": "Curator",
}

AGENT_LABELS_ZH: dict[str, str] = {
    "discovery": "机会发现",
    "analyze": "结构分析",
    "validate": "验证",
    "write": "写入",
    "explain": "解释",
    "portfolio": "持仓",
    "verdict": "综合裁决",
    "curator": "沉淀",
}

GUARDRAIL_LOCKED_AGENTS: frozenset[str] = frozenset({"validate"})

VALIDATE_NEUTRAL_APPENDIX = (
    "Neutral validation mandate (non-negotiable): stay independent; actively seek "
    "counter-evidence and falsification for user-biased hypotheses; never rubber-stamp "
    "preferred narratives."
)

__all__ = [
    "AGENT_LABELS",
    "AGENT_LABELS_ZH",
    "AGENT_NAMES",
    "AgentName",
    "GUARDRAIL_LOCKED_AGENTS",
    "VALIDATE_NEUTRAL_APPENDIX",
]
