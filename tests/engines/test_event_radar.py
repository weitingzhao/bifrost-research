"""Wave 4.3 Event Radar pipeline tests."""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.event_radar.pipeline import run_pipeline, step_parse


SAMPLE = """
- Fed announced rate hold on 2024-06-12; markets rally on $SPY.
- Sources say MegaCorp plans IPO next quarter.
- GDP calendar: 2024-07-25 consensus 2.1%.
- hi
- Fed announced rate hold on 2024-06-12; markets rally on $SPY.
"""


def test_parse_splits_bullets() -> None:
    raw = step_parse(SAMPLE, source="unit", collected_at=date(2024, 6, 12))
    assert len(raw) >= 4
    assert all(r.raw_text for r in raw)


def test_pipeline_dedupes_and_self_checks() -> None:
    result = run_pipeline(SAMPLE, source="unit", collected_at=date(2024, 6, 12))
    assert result.raw_count >= 4
    assert result.dropped  # duplicate + short noise
    assert result.export_rows
    assert result.self_check["passed"] is True
    # Verbatim raw text preserved
    assert any("Fed announced" in r["raw_text"] for r in result.export_rows)
    d = result.to_dict()
    assert "D10" in d["advisory"]


def test_theme_matcher_lines() -> None:
    """Names mirror the workspace registry; unmatched stays blank (no catch-all)."""
    from bifrost_research.engines.event_radar.pipeline import _match_theme

    assert _match_theme("2026-10-28 FOMC decision day per the Federal Reserve calendar.") == "利率路径重定价"
    assert _match_theme("2026-10-14 CPI release for September 2026, 8:30 a.m. Eastern.") == "利率路径重定价"
    assert _match_theme("RIOT filed an 8-K: Bitcoin Holdings Update as of November 30.") == "比特币国库"
    # A bond's coupon is not the rate path; a dividend is no theme at all.
    assert _match_theme("notes bearing interest at a rate of 5.25% due 2031") == ""
    assert _match_theme("2026-10-01 NVDA dividend payment ahead: 0.01 per share") == ""


def test_theme_reaches_export_rows() -> None:
    result = run_pipeline(
        "- 2026-10-28 FOMC decision day; the committee meets, per the Federal Reserve calendar.",
        source="unit",
        collected_at=date(2026, 9, 24),
    )
    themes = [r["theme"] for r in result.export_rows]
    assert themes == ["利率路径重定价"]
