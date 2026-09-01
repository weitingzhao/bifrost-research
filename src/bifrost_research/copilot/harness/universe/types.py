"""Universe resolver result types — Wave LS-2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FunnelStep:
    name: str
    in_count: int
    out_count: int
    filter_summary: str
    dropped_sample: list[str] = field(default_factory=list)
    optional: bool = False
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "in_count": self.in_count,
            "out_count": self.out_count,
            "filter": self.filter_summary,
        }
        if self.dropped_sample:
            out["dropped_sample"] = self.dropped_sample[:20]
        if self.optional:
            out["optional"] = True
        if self.skipped:
            out["skipped"] = True
            if self.skip_reason:
                out["skip_reason"] = self.skip_reason
        return out


@dataclass
class UniverseResult:
    symbols: list[str]
    row_meta_by_symbol: dict[str, dict[str, Any]]
    funnel: list[FunnelStep]
    data_source: str
    universe_mode: str
    option_overlay_applied: bool = False
    layer_results: dict[str, Any] = field(default_factory=dict)
    policy_warnings: list[str] = field(default_factory=list)

    def funnel_dicts(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.funnel]
