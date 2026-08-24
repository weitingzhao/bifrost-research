"""Paths for Dagster + dbt project resolution."""

from __future__ import annotations

from pathlib import Path

# bifrost_research package root (…/src/bifrost_research)
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

DBT_PROJECT_DIR = PACKAGE_ROOT / "dbt"
DBT_PROFILES_DIR = DBT_PROJECT_DIR
DBT_MANIFEST_PATH = DBT_PROJECT_DIR / "target" / "manifest.json"


def dbt_manifest_exists() -> bool:
    return DBT_MANIFEST_PATH.is_file()
