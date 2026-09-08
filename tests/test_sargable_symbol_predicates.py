"""Guard: no ``UPPER(TRIM(col))`` in predicate position in Python source.

Wrapping the column hides the btree index behind a function call, so a point
lookup degrades to a full scan. Measured on ``raw_market.stock_daily``
(13.6M rows): 3.2s wrapped vs 0.000s plain for one symbol+date lookup, and on
the ``raw_market.stock_financials`` union view 114.1s vs 0.6s. Every symbol
column the engines read is already stored upper-cased and untrimmed-clean
(``col IS DISTINCT FROM UPPER(TRIM(col))`` returns 0 rows on all of them), and
every caller normalises with ``.strip().upper()`` before binding — so the
wrapper buys nothing and costs the index. See ``repositories/opex_cycle.py``
for the long-form note.

Projection position (``SELECT UPPER(TRIM(symbol)) ...``) is fine and stays: it
normalises output rather than filtering input, so no index is at stake.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "bifrost_research"

# The wrapper introduced by WHERE / AND / OR — i.e. filtering, not projecting.
_PREDICATE_WRAP = re.compile(r"\b(?:WHERE|AND|OR)\s+UPPER\(TRIM\(")


def _iter_py_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def test_no_upper_trim_in_predicate_position() -> None:
    violations: list[str] = []
    for path in _iter_py_files():
        text = path.read_text(encoding="utf-8")
        if "UPPER(TRIM(" not in text:
            continue
        for match in _PREDICATE_WRAP.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            violations.append(f"{path.relative_to(_SRC_ROOT.parent.parent)}:{line}")
    assert not violations, (
        "UPPER(TRIM(col)) in predicate position hides the index — compare the "
        "column directly and normalise in the caller:\n" + "\n".join(violations)
    )
