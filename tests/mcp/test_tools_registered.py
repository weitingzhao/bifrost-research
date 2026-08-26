"""Wave RS-E2.1 — Research MCP tool registration tests (no live DB / SSE)."""

from __future__ import annotations

import re
from pathlib import Path

from bifrost_research.mcp.server import TOOL_NAMES, create_mcp_server, list_registered_tool_names
from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX

_MCP_ROOT = Path(__file__).resolve().parents[2] / "src" / "bifrost_research" / "mcp"
_MUTATION_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER)\b",
    re.IGNORECASE,
)
# Allow "DELETE" only inside the READ_ONLY_SUFFIX phrase "Does not modify data."
# and comments; scan SQL-ish statements by excluding description strings is hard —
# instead assert no mutation keywords appear outside string literals that mention
# read-only. Practical check: no raw SQL mutation verbs in non-string code tokens.
_CODE_MUTATION_RE = re.compile(
    r"""(?x)
    (?<!['"])           # not preceded by quote (rough)
    \b(INSERT\s+INTO|UPDATE\s+\w+|DELETE\s+FROM|DROP\s+TABLE|TRUNCATE\s+TABLE)\b
    """,
    re.IGNORECASE,
)


def test_tool_count_at_least_25() -> None:
    assert len(TOOL_NAMES) >= 25


def test_all_canonical_tools_registered() -> None:
    names = list_registered_tool_names()
    assert set(TOOL_NAMES).issubset(set(names))
    assert len(names) >= 25


def test_tool_names_follow_convention() -> None:
    for name in TOOL_NAMES:
        assert name.startswith("research.")
        parts = name.split(".")
        assert len(parts) >= 3


def test_every_tool_description_says_read_only() -> None:
    mcp = create_mcp_server()
    tools = mcp._tool_manager.list_tools()  # noqa: SLF001
    by_name = {t.name: t for t in tools}
    for name in TOOL_NAMES:
        tool = by_name[name]
        desc = tool.description or ""
        assert "Read-only" in desc or READ_ONLY_SUFFIX.split(".")[0] in desc
        assert "Does not modify data" in desc


def test_mcp_folder_has_no_sql_mutations() -> None:
    """Grep assert: mcp/ tools use SELECT-only paths (no INSERT/UPDATE/DELETE SQL)."""
    offenders: list[str] = []
    for path in _MCP_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _CODE_MUTATION_RE.finditer(text):
            offenders.append(f"{path.relative_to(_MCP_ROOT.parent.parent)}:{match.group(0)}")
    assert offenders == [], f"mutation SQL in mcp/: {offenders}"


def test_create_mcp_server_idempotent() -> None:
    a = list_registered_tool_names()
    b = list_registered_tool_names()
    assert a == b
