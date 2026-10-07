"""TD-160: ddl.py must not recreate the pre-rename event_radar indexes.

The live duplicates are dropped by an Owner script. CI cannot see Golden Source,
so this test locks the script and refuses a second index on the same keys.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DDL = _ROOT / "src/bifrost_research/schema/ddl.py"
_DROP = _ROOT / "scripts/oneoff/2026-10-07-td160-drop-event-radar-dup-indexes.sql"


def test_old_index_names_are_not_created_by_ddl() -> None:
    text = _DDL.read_text()
    assert "CREATE INDEX IF NOT EXISTS event_radar_batch_collected" not in text
    assert "CREATE INDEX IF NOT EXISTS event_radar_importance" not in text
    assert "event_signal_radar_daily_batch_collected" in text
    assert "event_signal_radar_daily_importance" in text


def test_owner_script_drops_only_the_two_duplicates() -> None:
    text = _DROP.read_text()
    drops = re.findall(r"DROP INDEX CONCURRENTLY IF EXISTS (\S+);", text)
    assert drops == [
        "features.event_radar_batch_collected",
        "features.event_radar_importance",
    ]
    assert "event_radar_pkey" not in drops
    assert not re.search(r"(?m)^BEGIN\b", text)


def test_no_two_indexes_on_event_signal_radar_share_keys() -> None:
    text = _DDL.read_text()
    defs = re.findall(
        r"CREATE INDEX IF NOT EXISTS (\w+)\s+ON [^(\n]*event_signal_radar_daily \(([^)]*)\)",
        text,
    )
    keys = [cols.strip() for _, cols in defs]
    assert keys
    assert len(keys) == len(set(keys))
