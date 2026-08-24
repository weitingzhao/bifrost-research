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
