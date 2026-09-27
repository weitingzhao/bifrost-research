"""The OpEx pin reads the next monthly expiry everywhere (Owner 2026-09-26)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from bifrost_research.lenses.pin_expiry import monthly_expiry_sql

SRC = Path(__file__).resolve().parents[2] / "src" / "bifrost_research"


def test_the_predicate_is_the_third_friday_or_the_thursday_a_holiday_moves_it_to() -> None:
    sql = monthly_expiry_sql("m.expiry", "m.trade_date")
    assert "m.expiry > m.trade_date" in sql, "on OpEx day the cycle has closed"
    assert "extract(dow from m.expiry) = 5 AND extract(day from m.expiry) BETWEEN 15 AND 21" in sql
    assert "extract(day from m.expiry + 1) BETWEEN 15 AND 21" in sql
    assert "h.holiday_date = m.expiry + 1 AND h.status = 'closed'" in sql


def _strings(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append("".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)))
    return out


def test_no_max_pain_reader_picks_the_expiry_nearest_30_days() -> None:
    """Four readers (exhibit, screen, signal-hit, similar-regime) and the scan
    had each written the same nearest-30-DTE pick; one of them reverting would
    split the reading from its record again."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if "max_pain" not in text:
            continue
        for s in _strings(ast.parse(text)):
            # One statement can hold several CTEs (the scan reads GEX at ~30
            # DTE beside the pin, rightly); judge each CTE on its own.
            for part in re.split(r"\bAS\s*\(", s):
                reads_max_pain = "option_metric_max_pain_daily" in part or "max_pain_strike" in part
                if reads_max_pain and "- 30)" in part:
                    offenders.append(str(path.relative_to(SRC)))
    assert offenders == []
