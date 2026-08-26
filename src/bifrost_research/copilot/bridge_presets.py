"""Bridge preset catalog — Wave RS-EX2-P0."""

from __future__ import annotations

from typing import Any, Literal

BridgeFocus = Literal[
    "portfolio_risk",
    "strategy_validation",
    "event_driven",
    "coding_landing",
]
BridgeDepth = Literal["brief", "standard", "deep"]
BridgeTarget = Literal["chatgpt", "claude", "deepseek", "generic"]

FOCUS_LABELS: dict[str, str] = {
    "portfolio_risk": "Portfolio & risk",
    "strategy_validation": "Strategy validation",
    "event_driven": "Event-driven context",
    "coding_landing": "Coding / implementation landing",
}

DEPTH_LABELS: dict[str, str] = {
    "brief": "Brief — bullet summary only",
    "standard": "Standard — structured sections",
    "deep": "Deep — sections + open questions",
}

TARGET_LABELS: dict[str, str] = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "deepseek": "DeepSeek",
    "generic": "Generic markdown",
}

FOCUS_HINTS: dict[str, str] = {
    "portfolio_risk": "Emphasize holdings, Greeks, P&L, hedge activity, and risk limits.",
    "strategy_validation": "Emphasize instances, opportunities, gates, and structure fit.",
    "event_driven": "Emphasize event radar, OpEx, catalysts, and timing.",
    "coding_landing": "Emphasize actionable specs, APIs, and implementation steps for engineers.",
}

DEFAULT_BRIDGE_MODEL = "deepseek-chat"
DEFAULT_BRIDGE_FOCUS: BridgeFocus = "portfolio_risk"
DEFAULT_BRIDGE_DEPTH: BridgeDepth = "standard"
DEFAULT_BRIDGE_TARGET: BridgeTarget = "deepseek"


def list_presets() -> dict[str, Any]:
    return {
        "focuses": [
            {"id": k, "label": FOCUS_LABELS[k], "hint": FOCUS_HINTS[k]}
            for k in FOCUS_LABELS
        ],
        "depths": [{"id": k, "label": DEPTH_LABELS[k]} for k in DEPTH_LABELS],
        "targets": [{"id": k, "label": TARGET_LABELS[k]} for k in TARGET_LABELS],
        "default_model": DEFAULT_BRIDGE_MODEL,
        "default_focus": DEFAULT_BRIDGE_FOCUS,
        "default_depth": DEFAULT_BRIDGE_DEPTH,
        "default_target": DEFAULT_BRIDGE_TARGET,
    }


def validate_focus(value: str) -> BridgeFocus:
    if value not in FOCUS_LABELS:
        raise ValueError(f"invalid focus: {value}")
    return value  # type: ignore[return-value]


def validate_depth(value: str) -> BridgeDepth:
    if value not in DEPTH_LABELS:
        raise ValueError(f"invalid depth: {value}")
    return value  # type: ignore[return-value]


def validate_target(value: str) -> BridgeTarget:
    if value not in TARGET_LABELS:
        raise ValueError(f"invalid target: {value}")
    return value  # type: ignore[return-value]


__all__ = [
    "DEFAULT_BRIDGE_DEPTH",
    "DEFAULT_BRIDGE_FOCUS",
    "DEFAULT_BRIDGE_MODEL",
    "DEFAULT_BRIDGE_TARGET",
    "DEPTH_LABELS",
    "FOCUS_HINTS",
    "FOCUS_LABELS",
    "TARGET_LABELS",
    "list_presets",
    "validate_depth",
    "validate_focus",
    "validate_target",
]
