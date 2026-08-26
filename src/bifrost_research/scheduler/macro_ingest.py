"""Macro economic calendar CSV ingest (manual drop zone, Wave R4)."""

from __future__ import annotations

import csv
import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from bifrost_research.db.conn import connect
from bifrost_research.db.upsert import batch_upsert

_MACRO_COLS = (
    "macro_id",
    "event_date",
    "release_ts",
    "country",
    "indicator",
    "actual_value",
    "expected_value",
    "prior_value",
    "unit",
    "gap_pct",
    "forward_flag",
    "source",
    "notes",
    "computed_at",
)


def _parse_float(val: str | None) -> float | None:
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(str(val).replace("%", "").strip())
    except ValueError:
        return None


def ingest_macro_csv(path: Path, *, source: str = "csv_drop") -> dict[str, Any]:
    rows: list[tuple[Any, ...]] = []
    now = datetime.now(timezone.utc)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=1):
            indicator = (row.get("indicator") or row.get("subject") or row.get("event") or "").strip()
            if not indicator:
                continue
            event_date_raw = (row.get("event_date") or row.get("date") or "").strip()
            event_date = date.fromisoformat(event_date_raw[:10]) if event_date_raw else date.today()
            actual = _parse_float(row.get("actual") or row.get("actual_value"))
            expected = _parse_float(row.get("expected") or row.get("expected_value"))
            prior = _parse_float(row.get("prior") or row.get("prior_value"))
            gap = None
            if actual is not None and expected is not None and expected != 0:
                gap = round((actual - expected) / abs(expected), 6)
            forward = str(row.get("forward_flag") or row.get("forward") or "").lower() in (
                "1",
                "true",
                "yes",
                "y",
            )
            rows.append(
                (
                    f"macro-{uuid.uuid4().hex[:10]}",
                    event_date,
                    None,
                    (row.get("country") or "").strip() or None,
                    indicator,
                    actual,
                    expected,
                    prior,
                    (row.get("unit") or "").strip() or None,
                    gap,
                    forward,
                    source,
                    (row.get("notes") or "").strip() or None,
                    now,
                )
            )
    if not rows:
        return {"ok": False, "error": "no_rows", "path": str(path)}
    with connect() as conn:
        n = batch_upsert(
            conn,
            "features.macro_event_daily",
            _MACRO_COLS,
            rows,
            conflict_keys=("macro_id",),
            set_fetched_at=False,
        )
        conn.commit()
    return {"ok": True, "rows": n, "path": str(path)}


def run_macro_ingest_from_env() -> dict[str, Any]:
    input_dir = Path(
        os.environ.get(
            "MACRO_CALENDAR_INPUT_DIR",
            str(Path.home() / "Desktop/stocks/Research-workspace/macro_calendar/input"),
        )
    )
    if not input_dir.is_dir():
        return {"ok": False, "error": "input_dir_missing", "dir": str(input_dir)}
    results: list[dict[str, Any]] = []
    for csv_path in sorted(input_dir.glob("*.csv")):
        results.append(ingest_macro_csv(csv_path))
    return {"ok": True, "files": len(results), "results": results}


if __name__ == "__main__":
    print(run_macro_ingest_from_env())
