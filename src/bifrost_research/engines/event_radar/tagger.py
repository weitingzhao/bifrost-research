"""Event tagging — heuristic default with optional LLM provider (Wave R4)."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

from bifrost_research.engines.event_radar.pipeline import RawEvent, TaggedEvent, step_tag


class EventTagger(ABC):
    @abstractmethod
    def tag(self, raw_events: list[RawEvent]) -> list[TaggedEvent]:
        ...


class HeuristicEventTagger(EventTagger):
    def tag(self, raw_events: list[RawEvent]) -> list[TaggedEvent]:
        return step_tag(raw_events)


class LLMEventTagger(EventTagger):
    """Optional LLM enrichment — falls back to heuristic on any failure."""

    def __init__(self, provider: str | None = None) -> None:
        self.provider = (provider or os.environ.get("EVENT_RADAR_LLM_PROVIDER") or "").strip()

    def tag(self, raw_events: list[RawEvent]) -> list[TaggedEvent]:
        base = step_tag(raw_events)
        if not self.provider or self.provider.lower() in ("heuristic", "none"):
            return base
        # LLM path reserved — heuristic remains authoritative until provider wired.
        for row in base:
            row.self_check = {
                **(row.self_check or {}),
                "llm_provider": self.provider,
                "llm_enriched": False,
            }
        return base


def get_event_tagger() -> EventTagger:
    provider = os.environ.get("EVENT_RADAR_LLM_PROVIDER", "heuristic").strip().lower()
    if provider in ("openai", "anthropic", "ollama"):
        return LLMEventTagger(provider=provider)
    return HeuristicEventTagger()
