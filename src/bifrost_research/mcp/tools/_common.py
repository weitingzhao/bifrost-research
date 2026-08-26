"""Shared helpers for read-only MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from bifrost_research.db.conn import connect

T = TypeVar("T")

READ_ONLY_SUFFIX = "**Read-only**. Does not modify data."


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def with_conn(fn: Callable[[Any], T]) -> T | dict[str, Any]:
    """Open a DB connection, run ``fn``, always close. Returns err envelope on failure."""
    try:
        conn = connect()
    except Exception as exc:  # noqa: BLE001 — surface to LLM as tool error
        return err(f"database unavailable: {exc}")
    try:
        return fn(conn)
    except Exception as exc:  # noqa: BLE001
        return err(str(exc))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
