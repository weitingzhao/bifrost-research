"""Elementary dbt report — status + static HTML/assets.

``GET /analytics/elementary`` — JSON presence/mtime.
``GET /analytics/elementary/files/{path}`` — report directory (PVC ``/report``).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(prefix="/analytics", tags=["elementary"])

_DEFAULT_REPORT = Path("/report/elementary_report.html")
_DEFAULT_INDEX = "elementary_report.html"


def configured_report_path() -> Path:
    env = os.environ.get("ELEMENTARY_REPORT_PATH", "").strip()
    return Path(env) if env else _DEFAULT_REPORT


def _report_root() -> Path:
    return configured_report_path().parent


def resolve_report_file() -> Path | None:
    path = configured_report_path()
    return path if path.is_file() else None


def safe_report_file(relative: str) -> Path | None:
    """Resolve a file under the report directory; reject traversal."""
    rel = relative.strip().lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    root = _report_root().resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target if target.is_file() else None


@router.get("/elementary")
def elementary_status() -> dict[str, Any]:
    """Return Elementary HTML report path + mtime when present."""
    path = resolve_report_file()
    if path is None:
        return {
            "ok": False,
            "present": False,
            "path": str(configured_report_path()),
            "mtime": None,
        }
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return {
        "ok": True,
        "present": True,
        "path": str(path),
        "mtime": mtime.isoformat(),
    }


@router.get("/elementary/files")
@router.get("/elementary/files/{asset_path:path}")
def elementary_file(asset_path: str = "") -> FileResponse:
    """Serve the Elementary report (and sibling assets) from the report directory."""
    name = asset_path.strip() or _DEFAULT_INDEX
    path = safe_report_file(name)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail={
                "ok": False,
                "present": False,
                "error": "Elementary report not found",
                "hint": "Set ELEMENTARY_REPORT_PATH or mount analytics-report-pvc at /report",
                "path": str(_report_root() / name),
            },
        )
    media = "text/html; charset=utf-8" if path.suffix.lower() in {".html", ".htm"} else None
    return FileResponse(path, media_type=media)
