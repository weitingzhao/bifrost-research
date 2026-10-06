"""Macro economic calendar -> ``features.macro_event_daily`` (Wave R4, TD-151).

Two inputs, one table:

- the packaged calendar ``scheduler/data/macro_calendar.csv`` (FOMC decisions, CPI
  releases), source ``calendar_seed``. The subscription has no forward economic
  calendar (Massive ``/fed/v1`` serves past observations; Benzinga is 403), so
  this file is the maintained source. ``research_macro_calendar_job`` ingests it
  every Monday and its output check says when the dates run out.
- optional CSV drops (actual / expected / prior values) from
  ``MACRO_CALENDAR_INPUT_DIR``, source ``csv_drop``. Until 2026-10 these were the
  only input and no schedule ever ran them: the table stayed at 0 rows.

``macro_id`` is derived from (country, indicator, event_date), so a re-ingest
updates its rows instead of adding a copy. A seed row that was taken out of the
file (a rescheduled meeting) is deleted on the next run; drop rows are kept.
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import ny_today
from bifrost_research.db.conn import connect
from bifrost_research.db.upsert import batch_upsert

TABLE = "features.macro_event_daily"
SEED_SOURCE = "calendar_seed"
DROP_SOURCE = "csv_drop"
SEED_RESOURCE = "data/macro_calendar.csv"
#: The calendar must reach at least this far ahead (the output check's ERROR line).
HORIZON_DAYS = 30
#: Indicators last seen longer ago than this are history, not a series that ran out.
STALE_INDICATOR_DAYS = 60

_NY = ZoneInfo("America/New_York")

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


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def macro_id_for(event_date: date, country: str | None, indicator: str) -> str:
    """Stable key: the same release ingested twice is one row."""
    return f"macro-{_slug(country or 'xx')}-{_slug(indicator)}-{event_date.isoformat()}"


def _release_ts(event_date: date, raw: str | None) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    hh, mm = raw.split(":", 1)
    local = datetime.combine(event_date, time(int(hh), int(mm)), tzinfo=_NY)
    return local.astimezone(timezone.utc)


def _data_lines(text: str) -> list[str]:
    """CSV lines without ``#`` comments or blank lines."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def parse_macro_csv(
    text: str,
    *,
    source: str,
    now: datetime,
    default_forward: bool = False,
) -> list[tuple[Any, ...]]:
    """Rows for ``_MACRO_COLS`` from CSV text (header row required).

    ``default_forward`` marks rows without a ``forward_flag`` column as calendar
    entries (the seed); a drop file says per row.
    """
    rows: dict[str, tuple[Any, ...]] = {}
    reader = csv.DictReader(io.StringIO("\n".join(_data_lines(text))))
    for row in reader:
        indicator = (row.get("indicator") or row.get("subject") or row.get("event") or "").strip()
        if not indicator:
            continue
        event_date_raw = (row.get("event_date") or row.get("date") or "").strip()
        event_date = date.fromisoformat(event_date_raw[:10]) if event_date_raw else ny_today(now)
        country = (row.get("country") or "").strip() or None
        actual = _parse_float(row.get("actual") or row.get("actual_value"))
        expected = _parse_float(row.get("expected") or row.get("expected_value"))
        prior = _parse_float(row.get("prior") or row.get("prior_value"))
        gap = None
        if actual is not None and expected is not None and expected != 0:
            gap = round((actual - expected) / abs(expected), 6)
        flag_raw = row.get("forward_flag") or row.get("forward")
        forward = (
            default_forward
            if flag_raw is None or str(flag_raw).strip() == ""
            else str(flag_raw).strip().lower() in ("1", "true", "yes", "y")
        )
        macro_id = macro_id_for(event_date, country, indicator)
        rows[macro_id] = (
            macro_id,
            event_date,
            _release_ts(event_date, row.get("release_time_et")),
            country,
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
    return list(rows.values())


def seed_text() -> str:
    return resources.files("bifrost_research.scheduler").joinpath(SEED_RESOURCE).read_text(
        encoding="utf-8"
    )


def drop_dir() -> Path:
    return Path(
        os.environ.get(
            "MACRO_CALENDAR_INPUT_DIR",
            str(Path.home() / "Desktop/stocks/Research-workspace/macro_calendar/input"),
        )
    )


def ingest_macro_csv(path: Path, *, source: str = DROP_SOURCE) -> dict[str, Any]:
    """One drop file (CLI / Mac use)."""
    now = datetime.now(timezone.utc)
    rows = parse_macro_csv(path.read_text(encoding="utf-8-sig"), source=source, now=now)
    if not rows:
        return {"ok": False, "error": "no_rows", "path": str(path)}
    conn = connect()
    try:
        n = batch_upsert(conn, TABLE, _MACRO_COLS, rows, conflict_keys=("macro_id",), set_fetched_at=False)
    finally:
        conn.close()
    return {"ok": True, "rows": n, "path": str(path)}


def _horizons(cur: Any) -> dict[str, date]:
    cur.execute(
        f"SELECT indicator, max(event_date) FROM {TABLE} WHERE forward_flag GROUP BY indicator"
    )
    return {str(ind): d for ind, d in cur.fetchall() or [] if d is not None}


def run_macro_ingest(
    *,
    connect_fn: Callable[[], Any] = connect,
    now: datetime | None = None,
    input_dir: Path | None = None,
) -> dict[str, Any]:
    """Seed calendar (+ any drop files) -> table; returns rows and the horizon read back."""
    now = now or datetime.now(timezone.utc)
    seed = parse_macro_csv(seed_text(), source=SEED_SOURCE, now=now, default_forward=True)
    if not seed:
        raise ValueError(f"{SEED_RESOURCE} parsed to 0 rows")

    directory = input_dir or drop_dir()
    drop_files = sorted(directory.glob("*.csv")) if directory.is_dir() else []
    dropped: list[tuple[Any, ...]] = []
    for path in drop_files:
        dropped.extend(
            parse_macro_csv(path.read_text(encoding="utf-8-sig"), source=DROP_SOURCE, now=now)
        )

    conn = connect_fn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {TABLE} WHERE source = %s AND NOT (macro_id = ANY(%s))",
                (SEED_SOURCE, [r[0] for r in seed]),
            )
            pruned = int(cur.rowcount or 0)
        written = batch_upsert(
            conn,
            TABLE,
            _MACRO_COLS,
            [*seed, *dropped],
            conflict_keys=("macro_id",),
            set_fetched_at=False,
            auto_commit=False,
        )
        conn.commit()
        with conn.cursor() as cur:
            horizons = _horizons(cur)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "engine": "macro_calendar",
        "ok": True,
        "rows_written": written,
        "seed_rows": len(seed),
        "drop_files": len(drop_files),
        "drop_rows": len(dropped),
        "pruned_seed_rows": pruned,
        "today": ny_today(now).isoformat(),
        "horizon": max(horizons.values()).isoformat() if horizons else None,
        "horizon_by_indicator": {k: v.isoformat() for k, v in sorted(horizons.items())},
    }


def horizon_findings(result: dict[str, Any]) -> list[tuple[str, str]]:
    """("error" | "warn", text) when the forward calendar is running out.

    ERROR: nothing dated ``HORIZON_DAYS`` or more ahead — the panel goes empty
    within a month. WARN: one indicator (still current) ends inside that window.
    """
    today_raw = result.get("today")
    if not today_raw:
        return []
    today = date.fromisoformat(str(today_raw))
    line = today + timedelta(days=HORIZON_DAYS)
    fix = f"add the next published dates to scheduler/{SEED_RESOURCE}"
    out: list[tuple[str, str]] = []
    horizon = result.get("horizon")
    if not horizon or date.fromisoformat(str(horizon)) < line:
        out.append(("error", f"forward macro calendar ends {horizon or 'nowhere'} (< {line}); {fix}"))
    for indicator, last in _iter_horizons(result.get("horizon_by_indicator")):
        if today - timedelta(days=STALE_INDICATOR_DAYS) <= last < line:
            out.append(("warn", f"{indicator} ends {last} (< {line}); {fix}"))
    return out


def _iter_horizons(raw: Any) -> Iterable[tuple[str, date]]:
    if not isinstance(raw, dict):
        return []
    return [(str(k), date.fromisoformat(str(v))) for k, v in raw.items() if v]


def run_macro_ingest_from_env() -> dict[str, Any]:
    return run_macro_ingest()


if __name__ == "__main__":
    print(run_macro_ingest_from_env())
