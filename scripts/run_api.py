"""Run Bifrost Research API (uvicorn on port 8795)."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8795"))
    uvicorn.run(
        "bifrost_research.api.app:app",
        host=host,
        port=port,
        reload=os.environ.get("RELOAD", "").lower() in ("1", "true", "yes"),
    )


if __name__ == "__main__":
    main()
