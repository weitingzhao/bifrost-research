"""Ratchet (TD-113): broad ``except`` handlers that neither log nor raise may only fall.

``except Exception: rollback; pass`` around the playbook trigger log meant a broken
trigger table would stop the log with no trace, and readers would see "no
transitions". A handler that swallows must at least log (``logger.*``, ``log.*``,
``context.log.*``, ``logging.*``, ``warnings.warn``) or re-raise.

Scope: the code paths that write or judge data — engines, lenses, repositories,
db, scheduler, orchestration. Measured 101 before TD-92/94/97/113 (10-06), 91 after.
Lower SILENT_SWALLOW_BASELINE when you remove one; never raise it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import bifrost_research

SILENT_SWALLOW_BASELINE = 91

ROOT = Path(bifrost_research.__file__).parent
SCOPE = ("engines", "lenses", "repositories", "db", "scheduler", "orchestration")
BROAD = {"Exception", "BaseException"}
LOGGERS = {"logger", "log", "logging", "_log", "_logger", "LOG", "LOGGER", "warnings"}


def _broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        (isinstance(n, ast.Name) and n.id in BROAD) or (isinstance(n, ast.Attribute) and n.attr in BROAD)
        for n in names
    )


def _speaks(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            func, chain = node.func, []
            while isinstance(func, ast.Attribute):
                chain.append(func.attr)
                func = func.value
            if isinstance(func, ast.Name):
                chain.append(func.id)
            if LOGGERS & set(chain):
                return True
    return False


def silent_swallows() -> list[str]:
    out: list[str] = []
    for sub in SCOPE:
        for path in sorted((ROOT / sub).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ExceptHandler) and _broad(node) and not _speaks(node):
                    out.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return out


def test_silent_broad_excepts_only_fall() -> None:
    found = silent_swallows()
    assert len(found) <= SILENT_SWALLOW_BASELINE, (
        f"{len(found)} broad except handlers neither log nor raise "
        f"(baseline {SILENT_SWALLOW_BASELINE}). Log the exception or re-raise; newest: {found[-5:]}"
    )


def test_the_playbook_trigger_paths_are_not_silent() -> None:
    found = silent_swallows()
    assert not [f for f in found if f.startswith("engines/forecast/playbook.py")], found
