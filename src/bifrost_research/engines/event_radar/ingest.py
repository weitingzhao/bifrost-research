"""File-based Event Radar ingest (Owner decision A).

Reads unstructured news files from ``EVENT_RADAR_INPUT_DIR``, runs the
5-step pipeline, upserts ``features.event_signal_radar_daily``, and optionally archives
processed files so they are not reprocessed.

Aligned with Research-workspace offline convention:
  ``Research-workspace/事件雷达工作流/input/``

D10 BLOCKED — advisory OLAP writes only (D13).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from uuid import uuid4

from bifrost_research.engines.event_radar.pipeline import (
    PipelineResult,
    run_pipeline,
    upsert_events,
)

logger = logging.getLogger(__name__)

# K8s default mount; local Mac overrides via EVENT_RADAR_INPUT_DIR.
DEFAULT_INPUT_DIR = "/data/event-radar/input"
DEFAULT_ARCHIVE_DIR = "/data/event-radar/archive"

_SUPPORTED_SUFFIXES = {".txt", ".md", ".json", ".csv", ".eml"}
_SKIP_NAMES = {
    "放这里.md",
    "readme.md",
    ".gitkeep",
    ".ds_store",
}


@dataclass
class FileIngestResult:
    path: str
    source: str
    batch_id: str
    raw_count: int
    kept: int
    dropped: int
    rows_written: int
    archived: bool
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""


@dataclass
class DirectoryIngestSummary:
    input_dir: str
    archive_dir: str
    files_seen: int
    files_processed: int
    files_skipped: int
    files_failed: int
    rows_written: int
    results: list[FileIngestResult] = field(default_factory=list)
    advisory: str = "D10 BLOCKED — event radar is advisory only (D13 OLAP)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_dir": self.input_dir,
            "archive_dir": self.archive_dir,
            "files_seen": self.files_seen,
            "files_processed": self.files_processed,
            "files_skipped": self.files_skipped,
            "files_failed": self.files_failed,
            "rows_written": self.rows_written,
            "results": [
                {
                    "path": r.path,
                    "source": r.source,
                    "batch_id": r.batch_id,
                    "raw_count": r.raw_count,
                    "kept": r.kept,
                    "dropped": r.dropped,
                    "rows_written": r.rows_written,
                    "archived": r.archived,
                    "skipped": r.skipped,
                    "skip_reason": r.skip_reason,
                    "error": r.error,
                }
                for r in self.results
            ],
            "advisory": self.advisory,
        }


def resolve_input_dir(explicit: str | Path | None = None) -> Path:
    raw = explicit or os.environ.get("EVENT_RADAR_INPUT_DIR") or DEFAULT_INPUT_DIR
    return Path(raw).expanduser().resolve()


def resolve_archive_dir(
    input_dir: Path,
    explicit: str | Path | None = None,
) -> Path:
    raw = explicit or os.environ.get("EVENT_RADAR_ARCHIVE_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    sibling = input_dir.parent / "archive"
    if sibling != input_dir:
        return sibling.resolve()
    return Path(DEFAULT_ARCHIVE_DIR).expanduser().resolve()


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _source_from_path(path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-") or "file"
    return f"ws:{stem[:48]}"


def _extract_json_text(payload: Any) -> str:
    """Flatten common JSON news shapes into newline-separated text."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (int, float, bool)):
        return str(payload)
    if isinstance(payload, list):
        parts: list[str] = []
        for item in payload:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = _dict_text(item)
                if text:
                    parts.append(text)
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(payload, dict):
        for key in ("events", "items", "articles", "rows", "data"):
            if key in payload:
                return _extract_json_text(payload[key])
        return _dict_text(payload)
    return str(payload)


def _dict_text(obj: dict[str, Any]) -> str:
    for key in (
        "raw_text",
        "text",
        "body",
        "content",
        "headline",
        "title",
        "summary",
        "event",
    ):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Fall back to compact JSON line (still parseable as one fragment).
    return json.dumps(obj, ensure_ascii=False)


def read_file_payload(path: Path) -> str:
    suffix = path.suffix.lower()
    raw_bytes = path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    if suffix == ".json":
        try:
            return _extract_json_text(json.loads(text)).strip()
        except json.JSONDecodeError:
            return text
    return text


def list_input_files(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(input_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.name.lower() in _SKIP_NAMES or path.name in _SKIP_NAMES:
            continue
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        files.append(path)
    return files


def _archive_file(src: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = archive_dir / f"{stamp}_{src.name}"
    if dest.exists():
        dest = archive_dir / f"{stamp}_{uuid4().hex[:6]}_{src.name}"
    shutil.move(str(src), str(dest))
    return dest


def process_file(
    path: Path,
    *,
    collected_at: date | None = None,
    batch_id: str | None = None,
    upsert: bool = True,
    conn: Any | None = None,
    archive: bool = True,
    archive_dir: Path | None = None,
) -> FileIngestResult:
    source = _source_from_path(path)
    bid = batch_id or f"file-{uuid4().hex[:10]}"
    try:
        payload = read_file_payload(path)
    except OSError as exc:
        return FileIngestResult(
            path=str(path),
            source=source,
            batch_id=bid,
            raw_count=0,
            kept=0,
            dropped=0,
            rows_written=0,
            archived=False,
            error=str(exc),
        )

    if not payload.strip():
        return FileIngestResult(
            path=str(path),
            source=source,
            batch_id=bid,
            raw_count=0,
            kept=0,
            dropped=0,
            rows_written=0,
            archived=False,
            skipped=True,
            skip_reason="empty_payload",
        )

    result = run_pipeline(
        payload,
        source=source,
        collected_at=collected_at,
        batch_id=bid,
    )
    written = 0
    own_conn = False
    active = conn
    if upsert:
        try:
            if active is None:
                from bifrost_research.db.conn import connect

                active = connect()
                own_conn = True
            written = upsert_events(active, result)
        finally:
            if own_conn and active is not None:
                active.close()

    archived = False
    # Archive only after a successful pipeline pass (even if all rows dropped).
    if archive and archive_dir is not None:
        try:
            _archive_file(path, archive_dir)
            archived = True
        except OSError as exc:
            logger.warning("archive failed for %s: %s", path, exc)

    return FileIngestResult(
        path=str(path),
        source=source,
        batch_id=result.batch_id,
        raw_count=result.raw_count,
        kept=len(result.kept),
        dropped=len(result.dropped),
        rows_written=written,
        archived=archived,
    )


def ingest_directory(
    input_dir: str | Path | None = None,
    *,
    archive_dir: str | Path | None = None,
    collected_at: date | None = None,
    upsert: bool = True,
    archive: bool | None = None,
    conn: Any | None = None,
    paths: Sequence[Path] | None = None,
) -> DirectoryIngestSummary:
    """Process all new files under the input directory.

    Parameters
    ----------
    upsert:
        When False, run pipeline only (unit tests / dry-run).
    archive:
        When None, reads ``EVENT_RADAR_ARCHIVE`` (default true).
    """
    in_dir = resolve_input_dir(input_dir)
    arch_dir = resolve_archive_dir(in_dir, archive_dir)
    do_archive = _env_bool("EVENT_RADAR_ARCHIVE", True) if archive is None else archive

    summary = DirectoryIngestSummary(
        input_dir=str(in_dir),
        archive_dir=str(arch_dir),
        files_seen=0,
        files_processed=0,
        files_skipped=0,
        files_failed=0,
        rows_written=0,
    )

    if not in_dir.is_dir():
        logger.warning("event_radar input dir missing: %s", in_dir)
        return summary

    files = list(paths) if paths is not None else list_input_files(in_dir)
    summary.files_seen = len(files)

    own_conn = False
    active = conn
    if upsert and active is None and files:
        try:
            from bifrost_research.db.conn import connect

            active = connect()
            own_conn = True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "event_radar DB connect failed — aborting without archive: %s",
                exc,
            )
            summary.files_failed = len(files)
            for path in files:
                summary.results.append(
                    FileIngestResult(
                        path=str(path),
                        source=_source_from_path(path),
                        batch_id="",
                        raw_count=0,
                        kept=0,
                        dropped=0,
                        rows_written=0,
                        archived=False,
                        error=f"db_connect_failed: {exc}",
                    )
                )
            return summary

    try:
        for path in files:
            fr = process_file(
                path,
                collected_at=collected_at,
                upsert=upsert,
                conn=active,
                archive=do_archive,
                archive_dir=arch_dir if do_archive else None,
            )
            summary.results.append(fr)
            if fr.error:
                summary.files_failed += 1
            elif fr.skipped:
                summary.files_skipped += 1
            else:
                summary.files_processed += 1
                summary.rows_written += fr.rows_written
    finally:
        if own_conn and active is not None:
            active.close()

    return summary


def ingest_payloads(
    payloads: Iterable[tuple[str, str]],
    *,
    collected_at: date | None = None,
    upsert: bool = False,
    conn: Any | None = None,
) -> list[PipelineResult]:
    """Test helper: (source, text) → pipeline results without filesystem."""
    out: list[PipelineResult] = []
    for source, text in payloads:
        result = run_pipeline(text, source=source, collected_at=collected_at)
        if upsert:
            if conn is None:
                raise ValueError("conn required when upsert=True")
            upsert_events(conn, result)
        out.append(result)
    return out
