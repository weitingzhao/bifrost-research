"""Nightly embedding pipeline stub — RS-KB5 (pgvector optional)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run_embed_pipeline() -> dict[str, int]:
    """Placeholder until pgvector embeddings are configured in cluster."""
    logger.info("embed_pipeline skipped — keyword search available via /research/playbook/search")
    return {"embedded": 0, "skipped": 0}


if __name__ == "__main__":
    run_embed_pipeline()
