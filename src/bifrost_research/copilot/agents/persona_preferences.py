"""Structured preference slots for agent personas (Wave RS-PS2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SymbolClass = Literal["growth", "value", "event_driven", "income", "fixed_income"]
TimeHorizon = Literal["day", "swing_2w_8w", "position_gt_2m"]
StructureBias = Literal[
    "outright",
    "debit_spread",
    "credit_spread",
    "iron_condor",
    "collar",
    "iv_crush",
    "opex_pin",
]
FavorSignal = Literal[
    "breakout",
    "iv_crush",
    "opex_pin",
    "event_gap",
    "gex_flip",
    "flow_bull",
    "flow_bear",
]

AGENT_RELEVANT_SLOTS: dict[str, frozenset[str]] = {
    "discovery": frozenset({"symbol_class", "avoid_classes", "time_horizon", "favor_signals", "disfavor_signals"}),
    "analyze": frozenset({"structure_bias", "favor_signals", "disfavor_signals", "time_horizon"}),
    "validate": frozenset(),
    "write": frozenset({"time_horizon"}),
    "explain": frozenset(),
    "portfolio": frozenset(
        {
            "max_single_position_pct",
            "max_sector_concentration_pct",
            "hard_stop_dd_pct",
            "symbol_class",
            "avoid_classes",
        }
    ),
    "verdict": frozenset({"symbol_class", "favor_signals", "time_horizon", "structure_bias"}),
    "curator": frozenset({"symbol_class", "favor_signals"}),
}


class PersonaPreferences(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol_class: list[SymbolClass] = Field(default_factory=list)
    avoid_classes: list[str] = Field(default_factory=list)
    time_horizon: TimeHorizon | None = None
    structure_bias: list[StructureBias] = Field(default_factory=list)
    max_single_position_pct: float | None = Field(default=None, ge=0, le=100)
    max_sector_concentration_pct: float | None = Field(default=None, ge=0, le=100)
    hard_stop_dd_pct: float | None = Field(default=None, ge=0, le=100)
    favor_signals: list[FavorSignal] = Field(default_factory=list)
    disfavor_signals: list[str] = Field(default_factory=list)

    @field_validator("avoid_classes", "disfavor_signals", mode="before")
    @classmethod
    def _strip_strings(cls, v: Any) -> list[str]:
        if not v:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []


def parse_preferences(raw: dict[str, Any] | None) -> PersonaPreferences:
    if not raw:
        return PersonaPreferences()
    return PersonaPreferences.model_validate(raw)


def preferences_for_agent(agent_name: str, prefs: PersonaPreferences) -> dict[str, Any]:
    """Return only slots relevant to this agent."""
    allowed = AGENT_RELEVANT_SLOTS.get(agent_name, frozenset())
    data = prefs.model_dump()
    return {k: v for k, v in data.items() if k in allowed and v not in (None, [], "")}


__all__ = [
    "AGENT_RELEVANT_SLOTS",
    "PersonaPreferences",
    "parse_preferences",
    "preferences_for_agent",
]
