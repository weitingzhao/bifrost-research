"""Event Radar file ingest tests (no live DB)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from bifrost_research.engines.event_radar.ingest import (
    ingest_directory,
    list_input_files,
    read_file_payload,
)


def test_read_txt_and_json(tmp_path: Path) -> None:
    txt = tmp_path / "notes.txt"
    txt.write_text(
        "- Fed announced rate hold on 2024-06-12; markets rally on $SPY.\n"
        "- Sources say MegaCorp plans IPO next quarter.\n",
        encoding="utf-8",
    )
    js = tmp_path / "bundle.json"
    js.write_text(
        json.dumps(
            {
                "events": [
                    {"title": "Oil inventories draw unexpectedly on 2024-06-11"},
                    "GDP calendar: 2024-07-25 consensus 2.1%.",
                ]
            }
        ),
        encoding="utf-8",
    )
    assert "Fed announced" in read_file_payload(txt)
    payload = read_file_payload(js)
    assert "Oil inventories" in payload
    assert "GDP calendar" in payload


def test_list_skips_placeholder(tmp_path: Path) -> None:
    (tmp_path / "放这里.md").write_text("put files here", encoding="utf-8")
    (tmp_path / "README.md").write_text("skip", encoding="utf-8")
    good = tmp_path / "batch.md"
    good.write_text("- Fed announced pause; $QQQ beats.\n", encoding="utf-8")
    files = list_input_files(tmp_path)
    assert [p.name for p in files] == ["batch.md"]


def test_ingest_directory_dry_run_file_to_pipeline(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text(
        "- Fed announced rate hold on 2024-06-12; markets rally on $SPY.\n"
        "- Sources say MegaCorp plans IPO next quarter.\n"
        "- hi\n",
        encoding="utf-8",
    )
    summary = ingest_directory(
        tmp_path,
        upsert=False,
        archive=False,
        collected_at=date(2024, 6, 12),
    )
    assert summary.files_seen == 1
    assert summary.files_processed == 1
    assert summary.rows_written == 0  # dry-run
    fr = summary.results[0]
    assert fr.kept >= 1
    assert fr.dropped >= 1  # short noise
    assert fr.source.startswith("ws:")
    assert sample.exists()  # not archived


def test_ingest_archives_when_requested(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    archive_dir = tmp_path / "archive"
    input_dir.mkdir()
    src = input_dir / "one.txt"
    src.write_text(
        "- Official announced approval; $AAPL upgrade on 2024-06-12.\n",
        encoding="utf-8",
    )
    summary = ingest_directory(
        input_dir,
        archive_dir=archive_dir,
        upsert=False,
        archive=True,
        collected_at=date(2024, 6, 12),
    )
    # archive=True with upsert=False is still honored when explicitly passed
    assert summary.files_processed == 1
    assert not src.exists()
    archived = list(archive_dir.iterdir())
    assert len(archived) == 1
    assert archived[0].name.endswith("_one.txt")
