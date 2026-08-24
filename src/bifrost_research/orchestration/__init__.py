"""Dagster orchestration — Wave 5.1.

Unifies dbt (SEPA) + Python engines + AI forecast under one Definitions graph.

Dependency (documented)::

    Plugin market ingest (external) → dbt → Python analytics → AI forecast

Install optional extra::

    pip install -e ".[orchestration]"

Entrypoint::

    from bifrost_research.orchestration.definitions import defs
"""

from __future__ import annotations

__all__ = ["build_definitions", "defs"]


def __getattr__(name: str):
    # Lazy import so `import bifrost_research.orchestration` works without dagster
    # until Definitions are requested (default install has no [orchestration] extra).
    if name in {"defs", "build_definitions"}:
        from bifrost_research.orchestration.definitions import build_definitions, defs

        return defs if name == "defs" else build_definitions
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
