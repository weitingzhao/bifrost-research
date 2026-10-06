"""Ratchet (TD-163): a test that needs dagster skips without it.

``dagster`` is the ``[orchestration]`` extra. CI installs it; a plain dev venv
(``pip install -e ".[dev]"``) does not, and two tests that imported it with no
guard (test_alert_scan_rejudge, test_event_radar_runner) made every local run
report 2 failures on a clean main — red that trains people to ignore red.

Every import in tests/ that loads dagster — ``dagster`` / ``dagster_*`` itself,
or a ``bifrost_research`` module that imports it at module level, directly or
through another such module — must follow ``pytest.importorskip("dagster")``:
at module level before it, or earlier in the same function.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
PACKAGE = "bifrost_research"


def _is_dagster(name: str) -> bool:
    return name == "dagster" or name.startswith(("dagster.", "dagster_"))


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    base = module.split(".")
    base = base if is_package else base[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _top_level(body: list[ast.stmt]) -> list[ast.stmt]:
    """Statements a module runs on import: its body, through try / if (not TYPE_CHECKING)."""
    out: list[ast.stmt] = []
    for stmt in body:
        if isinstance(stmt, ast.If):
            test = stmt.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                continue
            if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
                continue
            out += _top_level(stmt.body) + _top_level(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            out += _top_level(stmt.body)
            for handler in stmt.handlers:
                out += _top_level(handler.body)
            out += _top_level(stmt.orelse) + _top_level(stmt.finalbody)
        else:
            out.append(stmt)
    return out


def _imported(node: ast.stmt, module: str = "", is_package: bool = False) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom):
        base = _resolve(node, module, is_package)
        # `from pkg import sub` loads pkg and, when it is one, pkg.sub.
        return [base, *(f"{base}.{a.name}" for a in node.names)]
    return []


def _source_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in (SRC / PACKAGE).rglob("*.py"):
        if "dbt" in path.relative_to(SRC).parts:
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for stmt in _top_level(tree.body):
            names.update(_imported(stmt, module, path.name == "__init__.py"))
        graph[module] = names
    return graph


def _needs_dagster(graph: dict[str, set[str]]) -> set[str]:
    def parents(name: str) -> list[str]:
        parts = name.split(".")
        return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]

    needs: set[str] = set()
    changed = True
    while changed:
        changed = False
        for module, names in graph.items():
            if module in needs:
                continue
            for name in names:
                loaded = [p for p in parents(name) if p in graph]
                if _is_dagster(name) or any(p in needs for p in loaded):
                    needs.add(module)
                    changed = True
                    break
    return needs


def _loads_dagster(names: list[str], needs: set[str]) -> bool:
    for name in names:
        if _is_dagster(name):
            return True
        parts = name.split(".")
        if any(".".join(parts[:i]) in needs for i in range(1, len(parts) + 1)):
            return True
    return False


def _is_guard(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "importorskip"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and sub.args[0].value == "dagster"
        ):
            return True
    return False


def _label(path: Path, lineno: int) -> str:
    try:
        return f"{path.relative_to(ROOT)}:{lineno}"
    except ValueError:
        return f"{path.name}:{lineno}"


def _unguarded(path: Path, needs: set[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    module_guard = min(
        (s.lineno for s in tree.body if not isinstance(s, scopes) and _is_guard(s)),
        default=None,
    )
    offenders: list[str] = []

    def visit(body: list[ast.stmt], guard_line: int | None) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A function body runs after the whole module: any module-level
                # guard covers it; otherwise its own (or an enclosing one's).
                inner = min((s.lineno for s in stmt.body if _is_guard(s)), default=None)
                if module_guard is not None:
                    visit(stmt.body, 0)
                else:
                    visit(stmt.body, guard_line if guard_line is not None else inner)
                continue
            if isinstance(stmt, ast.ClassDef):
                visit(stmt.body, guard_line)
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                loads = _loads_dagster(_imported(stmt), needs)
                if loads and (guard_line is None or guard_line > stmt.lineno):
                    offenders.append(_label(path, stmt.lineno))
                continue
            nested: list[ast.stmt] = []
            for field in ("body", "orelse", "finalbody"):
                part = getattr(stmt, field, None)
                if isinstance(part, list):
                    nested += [n for n in part if isinstance(n, ast.stmt)]
            for handler in getattr(stmt, "handlers", None) or []:
                nested += handler.body
            visit(nested, guard_line)

    visit(tree.body, module_guard)
    return offenders


def test_the_scan_sees_the_modules_that_load_dagster() -> None:
    needs = _needs_dagster(_source_graph())
    assert "bifrost_research.orchestration.definitions" in needs
    assert "bifrost_research.orchestration.asset_checks" in needs
    assert "bifrost_research.orchestration.runners" not in needs


def test_every_dagster_import_in_tests_is_guarded() -> None:
    needs = _needs_dagster(_source_graph())
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        offenders += _unguarded(path, needs)
    assert not offenders, (
        "imports that load dagster without pytest.importorskip('dagster') before them: "
        f"{offenders}"
    )


def test_the_two_tests_that_failed_would_have_been_caught(tmp_path: Path) -> None:
    needs = _needs_dagster(_source_graph())
    bad = tmp_path / "test_bad.py"
    bad.write_text(
        "def test_x():\n"
        "    from dagster import materialize\n"
        "\n"
        "def test_y():\n"
        "    from bifrost_research.orchestration.asset_checks import judge_output\n",
        encoding="utf-8",
    )
    good = tmp_path / "test_good.py"
    good.write_text(
        "import pytest\n"
        "def test_x():\n"
        "    pytest.importorskip('dagster')\n"
        "    from dagster import materialize\n",
        encoding="utf-8",
    )
    assert len(_unguarded(bad, needs)) == 2
    assert _unguarded(good, needs) == []
