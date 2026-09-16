"""Purging the canned Event Radar rows: report by default, delete only when asked (R9 C1)."""

from __future__ import annotations

from typing import Any

from bifrost_research.engines.event_radar import purge
from bifrost_research.engines.event_radar.placeholders import PLACEHOLDER_SQL


class _Cur:
    def __init__(self, owner: _Conn) -> None:
        self.owner = owner
        self.rowcount: int | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        self.owner.sql.append(sql)
        if sql.strip().upper().startswith("DELETE"):
            self.owner.deleted = True
            self.rowcount = sum(self.owner.counts.values())
            self.owner.counts = {}

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [(source, n) for source, n in sorted(self.owner.counts.items())]

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = dict(counts)
        self.sql: list[str] = []
        self.deleted = False
        self.commits = 0

    def cursor(self) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        pass


COUNTS = {"dagster-fallback": 13, "ws:k8s-smoke": 2, "ws:sample": 2}


def _conn(monkeypatch, counts: dict[str, int] = COUNTS) -> _Conn:
    conn = _Conn(counts)
    monkeypatch.setattr(purge, "connect", lambda: conn)
    return conn


def test_the_default_run_counts_and_deletes_nothing(monkeypatch) -> None:
    conn = _conn(monkeypatch)
    result = purge.run()
    assert result["mode"] == "dry_run"
    assert result["matched_by_source"] == COUNTS and result["matched"] == 17
    assert result["deleted"] == 0
    # Not "no rows deleted" — no DELETE was ever sent, and nothing was committed.
    assert conn.deleted is False
    assert not any(s.strip().upper().startswith("DELETE") for s in conn.sql)
    assert conn.commits == 0


def test_force_deletes_by_the_same_predicate_the_readers_exclude(monkeypatch) -> None:
    conn = _conn(monkeypatch)
    result = purge.run(force=True)
    assert result["mode"] == "deleted"
    assert result["matched_by_source"] == COUNTS
    assert result["deleted"] == 17 and result["remaining_by_source"] == {}
    delete_sql = next(s for s in conn.sql if s.strip().upper().startswith("DELETE"))
    # One predicate, shared with the read endpoints — not a second, wider copy.
    assert PLACEHOLDER_SQL in delete_sql
    assert "features.event_signal_radar_daily" in delete_sql
    assert conn.commits == 1


def test_an_already_clean_table_reports_zero(monkeypatch) -> None:
    _conn(monkeypatch, {})
    result = purge.run(force=True)
    assert result["matched"] == 0 and result["deleted"] == 0
    assert result["remaining_by_source"] == {}
