"""The market slot schedules are copied into the market-data plugin; a change here must reach it (TD-200).

Dagster fires every market-data slot (``orchestration/market_slot_schedules.py``),
and the plugin's queue dashboard scores each slot's adherence against its own
copy of the slot crons (``config/schedule.yaml`` in
bifrost-platform-plugin-market-data, held to its
``tests/fixtures/dagster_slot_roster.json`` by TD-191). That snapshot only
moves when someone regenerates it, so a schedule moved here used to leave the
plugin judging fires that no longer happen, with every test green.

``tests/fixtures/dagster_slot_roster.json`` here is the same file, byte for
byte: this test rebuilds it from ``api/schedule_roster.py`` (which
``test_definitions.py`` holds to the ScheduleDefinitions) the way the plugin's
``scripts/snapshot_dagster_roster.py`` does, and fails on any difference.
Neither repo's CI reads the other's checkout; the plugin's test compares the
two files only when a sibling bifrost-research checkout is present.

No dagster import: this runs in the base test environment too.
"""

from __future__ import annotations

import json
from pathlib import Path

from bifrost_research.api.schedule_roster import SCHEDULE_ROSTER

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests" / "fixtures" / "dagster_slot_roster.json"
SOURCE = "bifrost-research src/bifrost_research/api/schedule_roster.py"

HOW_TO_FIX = (
    "The market slot schedules changed. Update the plugin snapshot too: run "
    "bifrost-platform-plugin-market-data/scripts/snapshot_dagster_roster.py "
    "(--research-src <this checkout>), update the slot crons in the plugin's "
    "config/schedule.yaml and k8s/base/configmap-schedule.yaml to match, then copy "
    "its tests/fixtures/dagster_slot_roster.json over tests/fixtures/ here."
)


def _rendered() -> str:
    """The snapshot exactly as snapshot_dagster_roster.py writes it."""
    rows = [
        {"schedule": s.name, "timezone": s.tz, "cron": s.cron, "slots": list(s.market_slots)}
        for s in SCHEDULE_ROSTER
        if s.market_slots
    ]
    snap = {"_source": SOURCE, "schedules": sorted(rows, key=lambda r: r["schedule"])}
    return json.dumps(snap, indent=2) + "\n"


def _by_slot(schedules: list[dict]) -> dict[str, tuple[list[str], list[str]]]:
    out: dict[str, tuple[list[str], list[str]]] = {}
    for s in schedules:
        for slot in s["slots"]:
            crons, zones = out.setdefault(slot, ([], []))
            crons.append(s["cron"])
            if s["timezone"] not in zones:
                zones.append(s["timezone"])
    return {k: (sorted(c), sorted(z)) for k, (c, z) in out.items()}


def test_the_roster_still_has_its_market_slots() -> None:
    schedules = json.loads(_rendered())["schedules"]
    assert len(_by_slot(schedules)) >= 20, (
        "the roster lost its market slots; the next test would pass by accident"
    )


def test_market_slot_schedules_equal_the_snapshot_the_plugin_copies() -> None:
    want = SNAPSHOT.read_text(encoding="utf-8")
    got = _rendered()
    if got == want:
        return
    old = _by_slot(json.loads(want)["schedules"])
    new = _by_slot(json.loads(got)["schedules"])
    drift = {
        slot: {"snapshot": old.get(slot), "roster": new.get(slot)}
        for slot in sorted(old.keys() | new.keys())
        if old.get(slot) != new.get(slot)
    }
    detail = drift or "none per slot; schedule names or the file layout differ"
    raise AssertionError(f"{HOW_TO_FIX}\nslot (crons, zones) drift: {detail}")
