"""Loop policy v2 schema — Wave LS-1 Stock-first Harness.

Formal Pydantic models for ``objective.policy_json``.  ``parse_policy`` is
fail-soft: unknown top-level keys are preserved; nested layer keys are validated
when present.

D10 BLOCKED — policy describes research scan only.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

UniverseMode = Literal[
    "stock_composite",
    "sepa",
    "momentum",
    "events",
    "scan_legacy",
]

DEFAULT_UNIVERSE_MODE: UniverseMode = "scan_legacy"


class SepaLayerPolicy(BaseModel):
    stage: list[str] = Field(default_factory=lambda: ["SETUP", "PIVOT"])
    path: str | None = None
    grade: str | None = None
    min_score: float = Field(default=70.0, ge=0, le=100)
    required: bool = True

    @field_validator("stage", mode="before")
    @classmethod
    def _normalize_stage(cls, value: Any) -> list[str]:
        if value is None:
            return ["SETUP", "PIVOT"]
        if isinstance(value, str):
            return [value.strip().upper()]
        if isinstance(value, list):
            return [str(s).strip().upper() for s in value if str(s).strip()]
        return ["SETUP", "PIVOT"]


class MomentumLayerPolicy(BaseModel):
    grade: str | None = None
    path: str | None = None
    min_score: float | None = Field(default=None, ge=0, le=100)
    required: bool = False


class EventsLayerPolicy(BaseModel):
    min_importance: int = Field(default=2, ge=1, le=3)
    within_days: int = Field(default=5, ge=1, le=60)
    required: bool = False


class LayersPolicy(BaseModel):
    sepa: SepaLayerPolicy = Field(default_factory=SepaLayerPolicy)
    momentum: MomentumLayerPolicy = Field(default_factory=MomentumLayerPolicy)
    events: EventsLayerPolicy = Field(default_factory=EventsLayerPolicy)


class OptionOverlayPolicy(BaseModel):
    enabled: bool = False
    required: bool = False
    flag_filter: str | None = None
    min_composite: float | None = Field(default=None, ge=0, le=1)
    scan_preset: str = "neutral"


class LoopPolicy(BaseModel):
    """Parsed harness policy — superset of legacy scan keys."""

    universe_mode: UniverseMode = DEFAULT_UNIVERSE_MODE
    layers: LayersPolicy = Field(default_factory=LayersPolicy)
    option_overlay: OptionOverlayPolicy = Field(default_factory=OptionOverlayPolicy)
    # Legacy scan keys (scan_legacy + option overlay)
    preset: str = "neutral"
    flag_filter: str | list[str] | None = None
    min_composite_score: float | None = None
    min_hit_rate: float | None = None
    max_candidates: int = Field(default=3, ge=1, le=50)
    seed_symbols: list[str] = Field(default_factory=list)
    source: str = "harness"
    use_llm_plan: bool | None = None
    llm_model: str | None = None
    auto_validate: bool = False

    model_config = {"extra": "allow"}

    def is_stock_mode(self) -> bool:
        return self.universe_mode in {"stock_composite", "sepa", "momentum", "events"}

    def flag_filter_str(self) -> str | None:
        raw = self.flag_filter
        if raw is None:
            return None
        if isinstance(raw, list):
            parts = [str(x).strip() for x in raw if str(x).strip()]
            return ",".join(parts) if parts else None
        s = str(raw).strip()
        return s or None


def parse_policy(raw: dict[str, Any] | None) -> LoopPolicy:
    """Parse policy_json fail-soft; log validation issues and return best-effort model."""
    if not raw:
        return LoopPolicy()
    try:
        return LoopPolicy.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("parse_policy: validation failed (%s); using defaults + raw passthrough", exc)
        base = LoopPolicy()
        merged = base.model_dump()
        merged.update({k: v for k, v in raw.items() if k not in merged or v is not None})
        try:
            return LoopPolicy.model_validate(merged)
        except Exception:  # noqa: BLE001
            return base


def validate_policy_for_mode(policy: LoopPolicy) -> list[str]:
    """Return non-fatal policy warnings for Owner / trace."""
    warnings: list[str] = []
    # Composite scores are 0–100 (candidates read 82.8, 79.2). The frontend's
    # recommended template shipped min_composite_score: 0.55, which filters
    # nothing — a threshold two orders of magnitude below every row it gates.
    # Warn rather than reject: an existing objective may already hold this value,
    # and refusing to load it would be worse than saying so.
    if policy.min_composite_score is not None and 0 < policy.min_composite_score <= 1:
        warnings.append(
            f"min_composite_score={policy.min_composite_score} looks like a 0–1 fraction; "
            "composite scores are 0–100, so this filters nothing"
        )
    if policy.is_stock_mode() and policy.min_hit_rate is not None and not policy.flag_filter_str():
        warnings.append("min_hit_rate ignored in stock modes without flag_filter")
    if policy.is_stock_mode() and policy.universe_mode != "scan_legacy" and policy.preset != "neutral":
        if policy.option_overlay.enabled:
            pass  # preset applies to overlay only
        elif policy.preset not in ("neutral",):
            warnings.append("preset applies to scan_legacy/option_overlay only in stock modes")
    return warnings


def default_stock_composite_policy() -> dict[str, Any]:
    """Recommended LS-1 stock-first policy dict."""
    return LoopPolicy(
        universe_mode="stock_composite",
        layers=LayersPolicy(
            sepa=SepaLayerPolicy(stage=["SETUP", "PIVOT"], min_score=70.0, required=True),
            momentum=MomentumLayerPolicy(grade="A", required=False),
            events=EventsLayerPolicy(min_importance=2, within_days=5, required=False),
        ),
        option_overlay=OptionOverlayPolicy(
            enabled=True,
            required=False,
            flag_filter="iv_rank:hot",
        ),
        max_candidates=8,
        use_llm_plan=True,
        auto_validate=True,
    ).model_dump()


__all__ = [
    "DEFAULT_UNIVERSE_MODE",
    "EventsLayerPolicy",
    "LayersPolicy",
    "LoopPolicy",
    "MomentumLayerPolicy",
    "OptionOverlayPolicy",
    "SepaLayerPolicy",
    "UniverseMode",
    "default_stock_composite_policy",
    "parse_policy",
    "validate_policy_for_mode",
]
