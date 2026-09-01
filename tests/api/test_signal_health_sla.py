"""Weekend-aware Product asof SLA (no DB)."""

from datetime import datetime, timezone

from bifrost_research.api.signal_health import (
    FRESH_SLA_HOURS,
    WEEKEND_SLA_HOURS,
    freshness_sla_hours,
    freshness_status_from_age,
)


def test_weekday_uses_36h() -> None:
    tue = datetime(2026, 9, 1, 17, 0, tzinfo=timezone.utc)  # Tuesday
    assert freshness_sla_hours(tue) == FRESH_SLA_HOURS
    assert freshness_status_from_age(36.0, now=tue) == "fresh"
    assert freshness_status_from_age(36.6, now=tue) == "stale"


def test_monday_afternoon_uses_72h() -> None:
    mon = datetime(2026, 8, 31, 17, 25, tzinfo=timezone.utc)
    assert freshness_sla_hours(mon) == WEEKEND_SLA_HOURS
    assert freshness_status_from_age(36.6, now=mon) == "fresh"
    assert freshness_status_from_age(73.0, now=mon) == "stale"


def test_monday_after_22utc_back_to_36h() -> None:
    mon_night = datetime(2026, 8, 31, 22, 30, tzinfo=timezone.utc)
    assert freshness_sla_hours(mon_night) == FRESH_SLA_HOURS
    assert freshness_status_from_age(36.6, now=mon_night) == "stale"


def test_saturday_uses_72h() -> None:
    sat = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    assert freshness_sla_hours(sat) == WEEKEND_SLA_HOURS
