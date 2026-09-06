"""Exhibit contract — the object a page verdict and a Copilot answer are both built from.

Wave 15 gave it ``readings`` / ``history_summary`` / ``caveats``; research-loop-automation
Phase A2 adds ``verdict`` (band from the lens registry), ``track_record`` (how the lens'
triggers settled) and ``similar`` (what followed readings like this one). All additive.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Freshness = Literal["fresh", "stale", "missing"]

STALE_HOURS = 36.0


class ExhibitResponse(BaseModel):
    lens: str
    symbol: str
    as_of: str | None = None
    freshness: Freshness = "missing"
    readings: dict[str, Any] = Field(default_factory=dict)
    history_summary: dict[str, Any] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)
    # Phase A2 — from the lens registry and the settled record.
    lens_id: str | None = None
    verdict: dict[str, Any] | None = None
    track_record: dict[str, Any] | None = None
    similar: dict[str, Any] | None = None


def age_hours(ts: Any) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return (datetime.utcnow() - ts).total_seconds() / 3600.0
        return (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 3600.0
    return None


def freshness_from(ts: Any, has_row: bool) -> Freshness:
    if not has_row:
        return "missing"
    age = age_hours(ts)
    if age is None:
        return "fresh"  # have a row but no computed_at — treat as present
    return "fresh" if age <= STALE_HOURS else "stale"


def iso_date(d: Any) -> str | None:
    if d is None:
        return None
    if isinstance(d, date):
        return d.isoformat()
    return str(d)[:10]


def rollback_quietly(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        pass
