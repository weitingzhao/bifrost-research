"""Guard: a per-day writer keyed wider than (symbol, trade_date) must delete the day first.

``batch_upsert`` on (symbol, trade_date, expiry) — or any key with more columns
than the day — only ever adds and overwrites: a row a later re-walk no longer
produces (an expiry the filter now drops, a strike with no contract left) keeps
its old value beside the new ones. signal_hit, gex, flow, pcr and max_pain each
had this and were fixed by delete-then-insert; option_surface_iv_daily was the
last of them (TD-112, 2026-10-06).

The check is per module: a ``batch_upsert`` whose literal ``conflict_keys``
contain ``symbol`` and ``trade_date`` plus anything else needs a ``DELETE FROM``
in the same module, or an entry below saying why the table is append-only.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "bifrost_research"

#: (module, table expression) -> why merging is right for it.
_APPEND_ONLY = {
    ("engines/forecast/playbook.py", "'features.stock_signal_playbook_trigger_intraday'"):
        "trigger events: one row per firing (trigger_at), never re-derived",
    ("engines/forecast/terrain.py", "'features.stock_forecast_terrain_intraday'"):
        "intraday snapshots keyed by asof_ts: an append log by design",
    ("engines/alert_scan/entry.py", "TABLE_STOCK_SIGNAL_ALERT_DAILY"):
        "KNOWN GAP (reported 2026-10-06 with TD-112): a re-run keeps alerts that no longer fire",
}


def _wide_day_upserts() -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "batch_upsert(" not in text or "DELETE FROM" in text.upper():
            continue
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "batch_upsert":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            keys_node = kw.get("conflict_keys")
            if not isinstance(keys_node, (ast.Tuple, ast.List)):
                continue
            keys = {e.value for e in keys_node.elts if isinstance(e, ast.Constant)}
            if {"symbol", "trade_date"} < keys:
                table = node.args[1] if len(node.args) > 1 else kw.get("table")
                out.append((rel, ast.unparse(table) if table is not None else "?", node.lineno))
    return out


def test_wide_keyed_day_writers_replace_the_day() -> None:
    bad = [f"{rel}:{line} {table}" for rel, table, line in _wide_day_upserts() if (rel, table) not in _APPEND_ONLY]
    assert not bad, (
        "batch_upsert keyed wider than (symbol, trade_date) with no DELETE in the module — "
        "delete the day in the same transaction, or list the table in _APPEND_ONLY with a reason:\n"
        + "\n".join(bad)
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    live = {(rel, table) for rel, table, _ in _wide_day_upserts()}
    assert not set(_APPEND_ONLY) - live, set(_APPEND_ONLY) - live
