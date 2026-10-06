"""The dated-event calendar behind ``GET /research/events/calendar`` (TD-181).

Two stores, one response:

- **Macro** rows (FOMC decisions, CPI releases, ...) come from
  ``features.macro_event_daily`` — the maintained calendar that
  ``research_macro_calendar_job`` ingests every Monday (TD-151), keyed by
  ``macro_id`` so a re-ingest updates instead of adding a copy.
- **Everything else** dated in the event radar (``time_code = 2``: dividend
  dates and other forward events) still comes from
  ``features.event_signal_radar_daily``.

Until 2026-10-06 the macro rows came from a hand-dropped radar file
(``ws:macro-calendar-2026q4``, collected 2026-09-24, last date 2026-12-10). The
radar's ids hash the collection date (``pipeline._stable_id``), so every re-drop
of that file would have written the same releases again under new ids, and the
calendar ran dry after 12-10. Radar rows from ``ws:macro*`` sources are now
superseded: the calendar skips them and the file ingest no longer writes them.

The response keeps the radar row shape (the Trade frontend's ``EventRadarRow``);
macro rows carry it too, plus ``origin``/``indicator``/``release_ts``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from bifrost_research.engines.event_radar.pipeline import _match_theme
from bifrost_research.engines.event_radar.placeholders import PLACEHOLDER_SQL

#: Radar file sources the macro calendar replaced (``ws:<file stem>``).
SUPERSEDED_MACRO_PREFIX = "ws:macro"
#: psycopg ``%s`` query: the LIKE ``%`` is doubled; the literal is the constant above.
SUPERSEDED_MACRO_SQL = f"(source LIKE '{SUPERSEDED_MACRO_PREFIX}%%')"
#: Macro rows reach back this far, so a month view still shows this month's releases.
MACRO_LOOKBACK_DAYS = 31
MACRO_TABLE = "features.macro_event_daily"
RADAR_TABLE = "features.event_signal_radar_daily"

ORIGIN_MACRO = "macro_event_daily"
ORIGIN_RADAR = "event_radar"

RADAR_COLS = (
    "event_id",
    "batch_id",
    "collected_at",
    "source",
    "subject",
    "event_summary",
    "affected_symbols",
    "direction",
    "certainty",
    "sentiment",
    "theme",
    "importance",
    "event_date",
    "date_basis",
    "computed_at",
)
MACRO_COLS = (
    "macro_id",
    "event_date",
    "release_ts",
    "country",
    "indicator",
    "source",
    "notes",
    "computed_at",
)

#: Radar importance scale (1 low .. 3 high). An indicator not listed is 1.
MACRO_IMPORTANCE = {"FOMC rate decision": 3, "CPI": 2}


def is_superseded_macro_source(source: str | None) -> bool:
    """A radar source the macro calendar replaced — same rule as ``SUPERSEDED_MACRO_SQL``."""
    return bool(source) and str(source).startswith(SUPERSEDED_MACRO_PREFIX)


def macro_window_start(today: date) -> date:
    return today - timedelta(days=MACRO_LOOKBACK_DAYS)


def _iso(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _day(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()[:10]
    return text or None


def _as_dict(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def macro_calendar_row(macro: Mapping[str, Any]) -> dict[str, Any]:
    """One ``macro_event_daily`` row in the calendar's (radar) row shape."""
    day = _day(macro.get("event_date")) or ""
    indicator = str(macro.get("indicator") or "").strip()
    notes = str(macro.get("notes") or "").strip()
    # "2026-10-14 CPI release (September 2026)": the frontend's cell is the first
    # word after the date, and the radar theme matcher knows "CPI release".
    what = indicator if "decision" in indicator.lower() else f"{indicator} release"
    summary = f"{day} {what}" + (f" ({notes})" if notes else "")
    return {
        "event_id": macro.get("macro_id"),
        "batch_id": ORIGIN_MACRO,
        "collected_at": _day(macro.get("computed_at")),
        "source": macro.get("source"),
        "subject": f"{day} {indicator}",
        "event_summary": summary,
        "affected_symbols": "",
        "direction": 0,
        "certainty": 0,
        "sentiment": 0,
        "theme": _match_theme(summary),
        "importance": MACRO_IMPORTANCE.get(indicator, 1),
        "event_date": day,
        "date_basis": day,
        "computed_at": _iso(macro.get("computed_at")),
        # additive (TD-181)
        "origin": ORIGIN_MACRO,
        "indicator": indicator,
        "country": macro.get("country"),
        "release_ts": _iso(macro.get("release_ts")),
    }


def radar_calendar_row(radar: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: _iso(v) for k, v in radar.items()}
    out["collected_at"] = _day(radar.get("collected_at"))
    out["event_date"] = _day(radar.get("event_date"))
    out["origin"] = ORIGIN_RADAR
    return out


def _sort_key(row: Mapping[str, Any]) -> tuple[int, str, int]:
    day = row.get("event_date")
    importance = row.get("importance")
    return (0 if day else 1, str(day or ""), -int(importance or 0))


def read_event_calendar(conn: Any, *, limit: int, today: date) -> dict[str, Any]:
    """The calendar response: radar rows (macro sources skipped) + macro_event_daily rows."""
    since = macro_window_start(today)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(RADAR_COLS)}
            FROM {RADAR_TABLE}
            WHERE dropped IS DISTINCT FROM true
              AND time_code = 2
              AND NOT {PLACEHOLDER_SQL}
              AND NOT {SUPERSEDED_MACRO_SQL}
            ORDER BY event_date ASC NULLS LAST, importance DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        radar_raw = cur.fetchall() or []
        cur.execute(
            f"""
            SELECT COUNT(*) FILTER (WHERE {PLACEHOLDER_SQL}) AS placeholder_rows,
                   COUNT(*) FILTER (
                       WHERE NOT {PLACEHOLDER_SQL} AND {SUPERSEDED_MACRO_SQL}
                   ) AS superseded_rows
            FROM {RADAR_TABLE}
            WHERE dropped IS DISTINCT FROM true
              AND time_code = 2
            """
        )
        counts = cur.fetchone() or (0, 0)
        cur.execute(
            f"""
            SELECT {', '.join(MACRO_COLS)}
            FROM {MACRO_TABLE}
            WHERE event_date >= %s
            ORDER BY event_date ASC, release_ts ASC NULLS LAST, macro_id ASC
            """,
            (since,),
        )
        macro_raw = cur.fetchall() or []

    radar_rows = [radar_calendar_row(_as_dict(r, RADAR_COLS)) for r in radar_raw]
    # The SQL already skips them; the same rule in Python keeps the two from drifting.
    radar_rows = [r for r in radar_rows if not is_superseded_macro_source(r.get("source"))]
    macro_rows = [macro_calendar_row(_as_dict(r, MACRO_COLS)) for r in macro_raw]
    rows = sorted([*radar_rows, *macro_rows], key=_sort_key)[:limit]

    if isinstance(counts, Mapping):
        placeholder, superseded = counts.get("placeholder_rows"), counts.get("superseded_rows")
    else:
        placeholder, superseded = counts[0], counts[1]
    return {
        "rows": rows,
        "count": len(rows),
        "excluded_placeholder_rows": int(placeholder or 0),
        # additive (TD-181)
        "superseded_macro_rows": int(superseded or 0),
        "macro_rows": sum(1 for r in rows if r.get("origin") == ORIGIN_MACRO),
        "macro_read_path": MACRO_TABLE,
        "macro_window_start": since.isoformat(),
    }
