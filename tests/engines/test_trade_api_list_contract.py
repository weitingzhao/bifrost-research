"""TD-89 ratchet: Research reads Trade API lists through one helper, against the current shape.

Trade API 0.4.0 answers every list as ``{items, count, …}``. option_pinned kept
reading the retired ``attributions`` / ``executions`` keys, saw zero legs on
2026-10-06 and reported success, while its sibling copy in terrain_backfill had
already moved to ``items``. The fixtures under ``tests/fixtures/trade_api`` carry
the key set PROD api 0.9.0 returned that day (values invented); every reader of
these two routes must find rows in them, and no module but the shared client may
read a list key itself.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from bifrost_research.engines.forecast import terrain_backfill
from bifrost_research.engines.option_pinned import entry as pinned
from bifrost_research.mcp.tools._trade_api_client import TradeApiShapeError, list_items

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/trade_api"
SRC = ROOT / "src/bifrost_research"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


ATTRIBUTION = _load("executions_position_attribution.json")
EXECUTIONS = _load("executions.json")


def _get(base: str, path: str, params: Any = None) -> Any:
    if path == "/executions/position-attribution":
        return ATTRIBUTION
    assert path == "/executions"
    return EXECUTIONS


def test_the_recorded_answers_are_list_envelopes() -> None:
    for body in (ATTRIBUTION, EXECUTIONS):
        assert isinstance(body["items"], list) and body["count"] == len(body["items"]) > 0
        assert "attributions" not in body and "executions" not in body


def test_option_pinned_reads_rows_from_the_current_shape() -> None:
    held = pinned.load_held_legs(_get, "http://api")
    executions = pinned.load_option_executions(_get, "http://api", since=date(2024, 1, 1))
    assert len(held) == 2
    assert len(executions) == 2  # the stock fill is not an option


def test_terrain_backfill_reads_rows_from_the_current_shape() -> None:
    targets, stats = terrain_backfill.trade_targets(_get, "http://api")
    # Both lists were read: trade 101 has a fill and is still held, so it is no target.
    assert stats["instances_with_executions"] == 1
    assert stats["instances_holding"] == 2
    assert stats["closed_instances"] == 0 and targets == []


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"detail": "Not Found"}, "no items"),
        ({"items": None, "count": 0}, "not a list"),
        ({"items": [{}], "count": 2}, "count=2"),
        ([{"contract_key": "x"}], "not an object"),
        (None, "not an object"),
    ],
)
def test_an_answer_without_a_readable_list_raises(payload: Any, match: str) -> None:
    with pytest.raises(TradeApiShapeError, match=match):
        list_items(payload, "attributions")


def test_the_retired_key_is_read_only_without_items() -> None:
    assert list_items({"attributions": [{"a": 1}]}, "attributions") == [{"a": 1}]
    assert list_items({"items": [], "count": 0, "attributions": [{"a": 1}]}, "attributions") == []


# Reading a Trade API list key by hand is how the copies drifted apart.
_HAND_READ = re.compile(r"""(\.get\(|\[)\s*["'](attributions|executions)["']""")


def test_no_module_reads_a_trade_api_list_key_by_hand() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "_trade_api_client.py":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _HAND_READ.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], "read Trade API lists with list_items():\n" + "\n".join(offenders)
