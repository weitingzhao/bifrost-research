"""In-process Research MCP for CuratorRun — Wave LO-1.

Starts a localhost SSE server in a daemon thread so CronJob pods do not depend
on the separate ``research-mcp`` Deployment.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_started_port: int | None = None


def ensure_local_mcp_url(*, port: int | None = None, startup_wait_s: float = 1.0) -> str:
    """Bind FastMCP SSE on localhost; return ``http://127.0.0.1:{port}/sse``."""
    global _started_port
    resolved = port or int(os.environ.get("BIFROST_CURATOR_MCP_PORT", "18796"))
    with _lock:
        if _started_port == resolved:
            return f"http://127.0.0.1:{resolved}/sse"

        from bifrost_research.mcp.server import create_mcp_server

        mcp = create_mcp_server(host="127.0.0.1", port=resolved)

        def _run() -> None:
            try:
                mcp.run(transport="sse")
            except Exception:
                logger.exception("local MCP server stopped")

        thread = threading.Thread(target=_run, name="curator-local-mcp", daemon=True)
        thread.start()
        time.sleep(max(0.2, startup_wait_s))
        _started_port = resolved
        url = f"http://127.0.0.1:{resolved}/sse"
        logger.info("CuratorRun local MCP at %s", url)
        return url


def reset_local_mcp_for_tests() -> None:
    global _started_port
    with _lock:
        _started_port = None


__all__ = ["ensure_local_mcp_url", "reset_local_mcp_for_tests"]
