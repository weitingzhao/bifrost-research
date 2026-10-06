"""Empty Event Radar input must not invent a sample and write it."""

from __future__ import annotations

from pathlib import Path

import pytest

from bifrost_research.engines.event_radar.ingest import DirectoryIngestSummary
from bifrost_research.orchestration import runners


def _empty(tmp_path: Path) -> DirectoryIngestSummary:
    return DirectoryIngestSummary(
        input_dir=str(tmp_path),
        archive_dir=str(tmp_path / "archive"),
        files_seen=0,
        files_processed=0,
        files_skipped=0,
        files_failed=0,
        rows_written=0,
    )


def test_empty_directory_does_not_upsert(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "bifrost_research.engines.event_radar.ingest.ingest_directory",
        lambda *a, **k: _empty(tmp_path),
    )
    calls: list[int] = []
    monkeypatch.setattr(
        "bifrost_research.engines.event_radar.pipeline.upsert_events",
        lambda *a, **k: calls.append(1) or 0,
    )
    result = runners.run_event_radar(input_dir=str(tmp_path))
    assert result == {"engine": "event_radar", "mode": "idle", "files_seen": 0}
    assert calls == []


def test_explicit_dry_run_does_not_upsert(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "bifrost_research.engines.event_radar.ingest.ingest_directory",
        lambda *a, **k: _empty(tmp_path),
    )
    calls: list[int] = []
    monkeypatch.setattr(
        "bifrost_research.engines.event_radar.pipeline.upsert_events",
        lambda *a, **k: calls.append(1) or 0,
    )
    result = runners.run_event_radar(
        input_dir=str(tmp_path),
        sample_text="Fed officials signal rate pause. Tech mega-caps rally.",
    )
    assert result["mode"] == "dry_run"
    assert result["rows_written"] == 0
    assert calls == []


def test_files_present_still_file_ingest(monkeypatch, tmp_path: Path) -> None:
    summary = DirectoryIngestSummary(
        input_dir=str(tmp_path),
        archive_dir=str(tmp_path / "archive"),
        files_seen=1,
        files_processed=1,
        files_skipped=0,
        files_failed=0,
        rows_written=2,
    )
    monkeypatch.setattr(
        "bifrost_research.engines.event_radar.ingest.ingest_directory",
        lambda *a, **k: summary,
    )
    result = runners.run_event_radar(input_dir=str(tmp_path))
    assert result["mode"] == "file_ingest"
    assert result["files_seen"] == 1
    assert result["files_processed"] == 1
    assert result["rows_written"] == 2


def test_a_missing_input_directory_raises(tmp_path: Path) -> None:
    """The cluster had no input mount and answered "idle" for weeks (TD-100)."""
    import pytest

    with pytest.raises(FileNotFoundError):
        runners.run_event_radar(input_dir=str(tmp_path / "not-mounted"))


def test_the_scheduled_event_radar_asset_runs_the_sec_ingest(monkeypatch) -> None:
    pytest.importorskip("dagster")
    from dagster import materialize

    from bifrost_research.orchestration.research_aux_schedules import engines_event_radar_sched

    calls: list[int] = []
    monkeypatch.setattr(
        runners, "run_event_radar_sec", lambda: calls.append(1) or {"mode": "sec_8k", "lines_new": 0}
    )
    monkeypatch.setattr(
        runners, "run_event_radar", lambda **k: (_ for _ in ()).throw(AssertionError("file ingest"))
    )
    assert materialize([engines_event_radar_sched]).success
    assert calls == [1]
