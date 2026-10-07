"""TD-252: every file dbt reads ships in the wheel.

The container installs the package non-editable, so a dbt path missing from
``[tool.setuptools.package-data]`` simply is not there. 0.180.0 added the first
generic test under ``dbt/tests/generic/``; package-data listed models, macros,
seeds and snapshots but not tests, and research_trading_day failed to compile
on 2026-10-07 while every local run (editable install) passed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "bifrost_research"
DBT = PKG / "dbt"

_PATH_KEYS = ("model-paths", "test-paths", "seed-paths", "macro-paths", "snapshot-paths", "analysis-paths")


def _shipped() -> set[Path]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = cfg["tool"]["setuptools"]["package-data"]["bifrost_research"]
    out: set[Path] = set()
    for pat in patterns:
        out.update(p for p in PKG.glob(pat) if p.is_file())
    return out


def _read_by_dbt() -> set[Path]:
    project = yaml.safe_load((DBT / "dbt_project.yml").read_text())
    files: set[Path] = set()
    for key in _PATH_KEYS:
        for rel in project.get(key, []):
            base = DBT / rel
            if base.is_dir():
                files.update(p for p in base.rglob("*") if p.is_file() and p.suffix in {".sql", ".yml", ".csv"})
    return files


def test_every_dbt_source_file_is_package_data() -> None:
    read = _read_by_dbt()
    assert any(p.parent.name == "generic" for p in read), "fixture: the generic tests moved; update this test"
    missing = sorted(str(p.relative_to(PKG)) for p in read - _shipped())
    assert missing == []
