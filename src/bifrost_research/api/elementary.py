"""Elementary dbt report status — ``GET /analytics/elementary``."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/analytics", tags=["elementary"])

_DEFAULT_REPORT = Path("/report/elementary_report.html")


def _resolve_report_path() -> Path | None:
    env = os.environ.get("ELEMENTARY_REPORT_PATH", "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    if _DEFAULT_REPORT.is_file():
        return _DEFAULT_REPORT
    return None


@router.get("/elementary")
def elementary_status() -> dict[str, Any]:
    """Return Elementary HTML report path + mtime when present."""
    path = _resolve_report_path()
    if path is None:
        return {
            "ok": False,
            "present": False,
            "path": os.environ.get("ELEMENTARY_REPORT_PATH") or str(_DEFAULT_REPORT),
            "mtime": None,
        }
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return {
        "ok": True,
        "present": True,
        "path": str(path),
        "mtime": mtime.isoformat(),
    }
