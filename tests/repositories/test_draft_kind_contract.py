"""Every draft kind the code inserts must be one the repository accepts.

policy_suggestion shipped in Wave Y.3 without being added to _ALLOWED_KINDS.
Nothing caught it for months because the only path that emits it needs
use_llm_plan, which scan_legacy leaves off — so the first stock_composite run
died on `invalid ai_draft kind`. This derives the expectation from the call
sites instead of restating the list, so the next kind cannot be added in one
place only.
"""

from __future__ import annotations

import ast
import pathlib

from bifrost_research.repositories.ai_draft import _ALLOWED_KINDS

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "bifrost_research"


def _inserted_kinds() -> dict[str, list[str]]:
    """Literal `kind=` arguments passed to any insert_draft(...) call."""
    found: dict[str, list[str]] = {}
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "insert_draft":
                continue
            for kw in node.keywords:
                if kw.arg == "kind" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        found.setdefault(kw.value.value, []).append(
                            str(path.relative_to(SRC))
                        )
    return found


def test_every_inserted_kind_is_allowed() -> None:
    kinds = _inserted_kinds()
    assert kinds, "found no insert_draft call sites — the scan is broken, not the code"
    unknown = {k: v for k, v in kinds.items() if k not in _ALLOWED_KINDS}
    assert not unknown, f"insert_draft kinds missing from _ALLOWED_KINDS: {unknown}"


def test_policy_suggestion_is_reachable_and_allowed() -> None:
    """The specific regression: the harness emits it, so it must be accepted."""
    kinds = _inserted_kinds()
    assert "policy_suggestion" in kinds
    assert "policy_suggestion" in _ALLOWED_KINDS


def test_action_kinds_are_not_confused_with_draft_kinds() -> None:
    """`harness_*` values are action_kind on the audit log, a different vocabulary."""
    assert "harness_candidate_batch" not in _ALLOWED_KINDS
    assert "harness_policy_suggestion" not in _ALLOWED_KINDS
