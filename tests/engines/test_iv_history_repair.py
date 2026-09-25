"""IV history repair: chunking, and a purge that reads only the reconstructed table."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from bifrost_research.engines.volatility import iv_history_repair as repair


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.rowcount = 0
        self._one: Any = None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.sql.append(sql)
        if "MIN(DATE(timezone" in sql:
            self._one = (self.conn.raw_first,)
        elif "MIN(trade_date)" in sql:
            self._one = (self.conn.oldest,)
        elif sql.lstrip().startswith("SELECT COUNT(*)"):
            queue = self.conn.fresh if "computed_at >= %s" in sql else self.conn.unconfirmed
            self._one = (queue.pop(0) if queue else 0,)
        elif sql.lstrip().startswith("DELETE"):
            self.rowcount = self.conn.deleted.pop(0) if self.conn.deleted else 0

    def fetchone(self) -> Any:
        return self._one

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class _Conn:
    def __init__(
        self,
        *,
        raw_first: date | None,
        oldest: date | None,
        unconfirmed: list[int] | None = None,
        fresh: list[int] | None = None,
        deleted: list[int] | None = None,
    ) -> None:
        self.raw_first, self.oldest = raw_first, oldest
        self.unconfirmed = unconfirmed or []
        self.fresh = fresh or []
        self.deleted = deleted or []
        self.sql: list[str] = []
        self.commits = self.rollbacks = 0

    def cursor(self) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _deletes(conn: _Conn) -> list[str]:
    return [s for s in conn.sql if s.lstrip().startswith("DELETE")]


def test_windows_cover_the_range_without_overlap() -> None:
    chunks = list(repair.windows(date(2026, 8, 1), date(2026, 8, 20), 7))
    assert chunks == [
        (date(2026, 8, 1), date(2026, 8, 7)),
        (date(2026, 8, 8), date(2026, 8, 14)),
        (date(2026, 8, 15), date(2026, 8, 20)),
    ]


T0 = repair.P3_CUTOVER_TS.replace(month=10)  # the reprojection's start in these tests


def test_dry_run_counts_what_the_reprojection_will_leave() -> None:
    conn = _Conn(raw_first=repair.FIRST_OBSERVATION, oldest=date(2026, 9, 1), unconfirmed=[5, 3])
    out = repair.purge_unconfirmed(conn, apply=False)
    assert out["rows"] == 8
    assert all("NOT" in q and "v_option_snapshot_with_stock" in q for q in conn.sql if "COUNT(*)" in q)
    assert _deletes(conn) == []


def test_apply_deletes_rows_no_post_p3_projection_confirmed() -> None:
    conn = _Conn(raw_first=repair.FIRST_OBSERVATION, oldest=date(2026, 9, 1), unconfirmed=[40, 7], fresh=[900, 300], deleted=[40, 7])
    out = repair.purge_unconfirmed(conn, apply=True, reprojected_at=T0)
    assert out["rows"] == 47
    assert all("computed_at < %s" in s for s in _deletes(conn))
    assert conn.commits == 2


def test_apply_still_works_after_raw_retention_trims_the_first_observation() -> None:
    """The durable part: no raw lookup is needed to tell a fossil from a trimmed row."""
    conn = _Conn(raw_first=date(2026, 12, 1), oldest=date(2026, 9, 1), unconfirmed=[12, 0], deleted=[12])
    out = repair.purge_unconfirmed(conn, apply=True, reprojected_at=T0)
    assert out["rows"] == 12
    assert len(_deletes(conn)) == 1
    assert not any("option_snapshot" in s for s in conn.sql if "COUNT(*)" in s)


def test_apply_needs_a_reprojection_in_the_same_run() -> None:
    conn = _Conn(raw_first=repair.FIRST_OBSERVATION, oldest=date(2026, 9, 1))
    with pytest.raises(RuntimeError, match="needs a reprojection"):
        repair.purge_unconfirmed(conn, apply=True)


def test_apply_stops_when_an_observed_week_was_not_reprojected() -> None:
    conn = _Conn(raw_first=repair.FIRST_OBSERVATION, oldest=date(2026, 9, 1), unconfirmed=[100], fresh=[0])
    with pytest.raises(RuntimeError, match="nothing in it was reprojected"):
        repair.purge_unconfirmed(conn, apply=True, reprojected_at=T0)
    assert _deletes(conn) == []


def test_weeks_before_the_first_observation_need_no_reprojection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repair, "P3_CUTOVER", date(2026, 6, 29))
    conn = _Conn(raw_first=repair.FIRST_OBSERVATION, oldest=date(2026, 6, 22), unconfirmed=[3], deleted=[3])
    out = repair.purge_unconfirmed(conn, apply=True, reprojected_at=T0)
    assert out["rows"] == 3
    assert not any("computed_at >= %s" in q for q in conn.sql)


def test_apply_rolls_back_when_the_delete_touches_a_different_count() -> None:
    conn = _Conn(raw_first=repair.FIRST_OBSERVATION, oldest=date(2026, 9, 1), unconfirmed=[40], fresh=[10], deleted=[41])
    with pytest.raises(RuntimeError, match="rolled back"):
        repair.purge_unconfirmed(conn, apply=True, reprojected_at=T0)
    assert conn.rollbacks == 1
    assert conn.commits == 0


# ─── canonical PnL is rebuilt symbol by symbol over the daily window ───


class _CanonConn(_Conn):
    def __init__(self) -> None:
        super().__init__(raw_first=None, oldest=None)
        self.calls: list[tuple[str, Any]] = []


def test_canonical_rebuild_deletes_then_recomputes_each_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _CanonConn()

    def fake_cohort(c: Any, **kw: Any) -> dict[str, Any]:
        c.calls.append(("cohort", (tuple(kw["symbols"]), kw["lookback_months"], kw["coverage"])))
        return {"rows_written": 10}

    monkeypatch.setattr(repair, "run_canonical_cohort", fake_cohort)
    out = repair.rebuild_canonical_pnl(conn, ["NVDA", "PLTR"], date(2026, 9, 24), apply=True)
    deletes = [s for s in conn.sql if s.lstrip().startswith("DELETE")]
    assert len(deletes) == 2 and all("stock_signal_canonical_pnl_daily" in s for s in deletes)
    assert conn.calls == [("cohort", (("NVDA",), 6, False)), ("cohort", (("PLTR",), 6, False))]
    assert conn.commits == 2
    assert out["rows_written"] == 20


def test_canonical_dry_run_only_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _CanonConn()
    monkeypatch.setattr(repair, "run_canonical_cohort", lambda *a, **k: pytest.fail("dry run must not compute"))
    out = repair.rebuild_canonical_pnl(conn, ["NVDA"], date(2026, 9, 24), apply=False)
    assert out["applied"] is False
    assert not any(s.lstrip().startswith("DELETE") for s in conn.sql)
