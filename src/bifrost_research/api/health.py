"""Health endpoint for Research API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from bifrost_research import __version__

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_startup_ok = True
_startup_error: str | None = None


def run_startup_schema_guard() -> None:
    """Best-effort legacy schema guard — does not block process start."""
    global _startup_ok, _startup_error
    try:
        from bifrost_research.db.conn import connect
        from bifrost_research.db.schema_guard import assert_no_legacy_schemas

        conn = connect()
        try:
            assert_no_legacy_schemas(conn)
            _startup_ok = True
            _startup_error = None
        finally:
            conn.close()
    except Exception as exc:
        _startup_ok = False
        _startup_error = str(exc)
        logger.error("startup schema guard failed: %s", exc)


@router.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok" if _startup_ok else "degraded",
        "startup_ok": _startup_ok,
        "startup_error": _startup_error,
        "version": __version__,
        "domain": "research",
    }
