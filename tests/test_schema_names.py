"""Guard: no legacy Golden Source feature schema qualifiers in Python source."""

from __future__ import annotations

import re
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "bifrost_research"

_FORBIDDEN_PATTERNS = (
    re.compile(r"\bsignals\."),
    re.compile(r"\bforecasts\."),
    re.compile(r"\bbacktests\."),
)


def _iter_py_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def test_no_legacy_feature_schema_qualifiers_in_src() -> None:
    violations: list[str] = []
    for path in _iter_py_files():
        text = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path.relative_to(_SRC_ROOT.parent.parent)}:{line}")
    assert not violations, "legacy schema qualifiers found:\n" + "\n".join(violations)
