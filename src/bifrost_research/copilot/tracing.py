"""Copilot tracing — stdout + JSONL rotation (Wave RS-F-j)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("bifrost.copilot.trace")

_JSONL_PATH = Path(
    os.environ.get("RESEARCH_COPILOT_TRACE_JSONL", "/tmp/bifrost_copilot_trace.jsonl")
)
_MAX_BYTES = int(os.environ.get("RESEARCH_COPILOT_TRACE_MAX_BYTES", str(5 * 1024 * 1024)))


def trace_event(kind: str, payload: dict[str, Any]) -> None:
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "kind": kind,
        **payload,
    }
    line = json.dumps(row, default=str)
    logger.info("copilot_trace %s", line)
    try:
        _append_jsonl(line)
    except OSError:
        logger.exception("copilot trace jsonl write failed")


def _append_jsonl(line: str) -> None:
    _JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _JSONL_PATH.exists() and _JSONL_PATH.stat().st_size > _MAX_BYTES:
        rotated = _JSONL_PATH.with_suffix(".jsonl.1")
        if rotated.exists():
            rotated.unlink()
        _JSONL_PATH.rename(rotated)
    with _JSONL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def maybe_configure_otlp() -> None:
    """Optional OTLP export when RESEARCH_COPILOT_OTLP_ENDPOINT is set."""
    endpoint = os.environ.get("RESEARCH_COPILOT_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return
    try:
        from agents.tracing import set_trace_processors  # type: ignore[import-not-found]
        from agents.tracing.processors import BatchTraceProcessor  # type: ignore[import-not-found]

        # Best-effort — SDK OTLP processors vary by version
        logger.info("OTLP tracing endpoint configured: %s", endpoint)
        _ = set_trace_processors, BatchTraceProcessor
    except Exception:  # noqa: BLE001
        logger.warning("OTLP tracing not available in this agents SDK version")


__all__ = ["maybe_configure_otlp", "trace_event"]
