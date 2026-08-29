"""OrderIntent payload schema — Wave O (advisory only, D10 BLOCKED)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LegSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    right: str | None = None  # C / P
    strike: float | None = None
    expiry: str | None = None
    side: str = "sell"  # buy / sell
    qty_hint: float | None = Field(default=None, description="Relative size hint, not absolute lots")


class SizingHint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kelly_frac: float | None = None
    risk_pct: float | None = None
    max_notional: float | None = None


class RiskHint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_dd: float | None = None
    invalidation_price: float | None = None
    notes: str | None = None


class OrderIntent(BaseModel):
    """Advisory proposal only — never placed as a live IB order (D10)."""

    model_config = ConfigDict(extra="ignore")

    hypothesis_id: str
    strategy_template: str = Field(..., description='e.g. "short_strangle_30d"')
    legs: list[LegSpec] = Field(default_factory=list)
    sizing_hint: SizingHint = Field(default_factory=SizingHint)
    risk_hint: RiskHint = Field(default_factory=RiskHint)
    expiry_at: datetime | None = None
    rationale: str = ""

    def to_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["advisory"] = True
        data["d10"] = "BLOCKED"
        return data
