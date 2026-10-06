"""alert_scan re-judges recent scan dates and runs after the scan it reads (TD-97).

composite_high never fired: the judge read each date once, at 22:30 UTC, four
hours before research_trading_day wrote that night's scan, and every
composite_score >= 90 row so far appeared on a later recompute of an earlier
date (META 08-31 written 09-03; AVGO/HUM/NKE/PEP/PSX/VLO 09-24 written 09-29).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from bifrost_research.engines.alert_scan import entry

SCAN = {
    date(2026, 9, 23): [],
    date(2026, 9, 24): [("AVGO", 91.2), ("PEP", 90.4)],
    date(2026, 9, 25): [],
    date(2026, 9, 28): [("META", 90.1)],
    date(2026, 9, 29): [],
}


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.rows: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.sql.append((" ".join(sql.split()), params))
        if "SELECT MAX(trade_date)" in sql:
            self.rows = [(max(SCAN),)]
        elif "SELECT DISTINCT trade_date" in sql:
            newest, n = params
            self.rows = [(d,) for d in sorted((d for d in SCAN if d <= newest), reverse=True)[:n]]
        elif "composite_score >= 90" in sql:
            self.rows = SCAN.get(params[0], [])
        elif sql.lstrip().startswith("DELETE"):
            self.conn.deleted.append(params)
            self.rowcount = 1
        else:
            self.rows = []

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return self.rows


class _Conn:
    def __init__(self) -> None:
        self.sql: list[tuple[str, Any]] = []
        self.deleted: list[Any] = []
        self.commits = 0

    def cursor(self) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> tuple[_Conn, list[Any]]:
    c = _Conn()
    upserts: list[Any] = []
    monkeypatch.setattr(entry, "connect", lambda: c)
    monkeypatch.setattr(entry, "batch_upsert", lambda _c, _t, _cols, rows, **kw: upserts.extend(rows) or len(rows))
    monkeypatch.setattr(entry, "LENSES", [])
    monkeypatch.setattr(
        "bifrost_research.db.calendar.latest_closed_session", lambda _c, **_k: date(2026, 9, 29)
    )
    return c, upserts


def test_a_later_recompute_of_an_earlier_date_fires(conn: tuple[_Conn, list[Any]]) -> None:
    c, upserts = conn
    result = entry.run()
    assert result["as_of"] == "2026-09-29" and result["session"] == "2026-09-29"
    assert result["judged_dates"] == ["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"]
    fired = {(r[0], r[2]) for r in upserts if r[1] == "composite_high"}
    assert fired == {(date(2026, 9, 24), "AVGO"), (date(2026, 9, 24), "PEP"), (date(2026, 9, 28), "META")}
    assert result["composite_high"] == {"2026-09-24": ["AVGO", "PEP"], "2026-09-28": ["META"]}


def test_every_judged_date_and_kind_is_replaced(conn: tuple[_Conn, list[Any]]) -> None:
    """A recompute below 90 takes the alert back: an upsert could only add."""
    c, _ = conn
    entry.run()
    composite = [p for p in c.deleted if p[1] == "composite_high"]
    assert [d for d, _k in composite] == sorted(SCAN)
    assert (date(2026, 9, 29), "hit_rate_drop") in c.deleted
    assert (date(2026, 9, 29), "weight_shift") in c.deleted
    # hit_rate_drop / weight_shift read trailing forward hits: newest date only.
    assert not [p for p in c.deleted if p[1] != "composite_high" and p[0] != date(2026, 9, 29)]


def test_a_dry_run_writes_nothing(conn: tuple[_Conn, list[Any]]) -> None:
    c, upserts = conn
    result = entry.run(dry_run=True)
    assert c.deleted == [] and upserts == []
    assert result["by_kind"]["composite_high"] == 3 and result["alerts_written"] == 0


def test_the_output_check_fails_when_the_judged_date_is_not_the_session() -> None:
    from bifrost_research.orchestration.asset_checks import judge_output
    from bifrost_research.orchestration.research_aux_schedules import ALERT_SCAN_SPEC

    stale, _ = judge_output({"as_of": "2026-09-28", "session": "2026-09-29"}, ALERT_SCAN_SPEC)
    assert stale and "2026-09-29" in stale[0][1]
    fresh, _ = judge_output({"as_of": "2026-09-29", "session": "2026-09-29"}, ALERT_SCAN_SPEC)
    assert fresh == []
