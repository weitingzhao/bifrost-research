"""Unit tests for alert_scan helpers (no DB)."""

from bifrost_research.api.alerts import VALID_KINDS


def test_valid_alert_kinds() -> None:
    assert "composite_high" in VALID_KINDS
    assert "weight_shift" in VALID_KINDS
    assert "hit_rate_drop" in VALID_KINDS
