"""Purging a retired listing's derived rows: which symbols, which dates, which layer.

Symbols, dates and counts here are invented. The tests pin the boundary (strictly
after the last bar), the two-signal refusal, the single transaction, and that the
raw layer is never written.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from bifrost_research.engines import retired_listing_purge as rp

TODAY = date(2031, 3, 20)
LAST = date(2031, 1, 10)


class _Cur:
    def __init__(self, parent: _Conn) -> None:
        self.parent = parent
        self.rowcount = 0
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        q = " ".join(str(sql).split())
        self.parent.statements.append((q, params))
        if "FROM pg_class" in q:
            self._rows = [(t,) for t in self.parent.tables]
            return
        if "raw_market.stock_daily" in q:
            self._rows = [self.parent.evidence.get(params[0], (None, None, None))]
            return
        if q.startswith("SELECT count(*)") or q.startswith("DELETE"):
            table = q.split("FROM features.")[1].split(" ")[0]
            n = self.parent.counts.get((table, params[0]), 0)
            if q.startswith("DELETE"):
                if table == self.parent.fail_on:
                    raise RuntimeError("boom")
                self.rowcount = n
            else:
                self._rows = [(n,)]
            return
        self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class _Conn:
    def __init__(
        self,
        *,
        tables: list[str],
        evidence: dict[str, tuple[Any, Any, Any]],
        counts: dict[tuple[str, str], int] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.tables = tables
        self.evidence = evidence
        self.counts = counts or {}
        self.fail_on = fail_on
        self.statements: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _retired(last: date = LAST) -> tuple[Any, Any, Any]:
    return (last, False, None)


def _writes(conn: _Conn) -> list[tuple[str, Any]]:
    return [s for s in conn.statements if s[0].startswith(("DELETE", "UPDATE", "INSERT"))]


def test_dry_run_counts_every_table_and_writes_nothing() -> None:
    conn = _Conn(
        tables=["alpha_daily", "beta_daily", "gamma_daily"],
        evidence={"ZZQ": _retired()},
        counts={("alpha_daily", "ZZQ"): 7, ("gamma_daily", "ZZQ"): 3},
    )
    out = rp.purge(conn, ["ZZQ"], apply=False, today=TODAY)

    assert out["applied"] is False
    assert out["tables_examined"] == 3
    assert out["symbols"]["ZZQ"]["rows"] == {"alpha_daily": 7, "gamma_daily": 3}
    assert out["total"] == 10
    assert _writes(conn) == []
    assert conn.commits == 0


def test_the_last_bar_session_survives() -> None:
    conn = _Conn(
        tables=["alpha_daily"], evidence={"ZZQ": _retired()}, counts={("alpha_daily", "ZZQ"): 2}
    )
    rp.purge(conn, ["ZZQ"], apply=True, today=TODAY)

    deletes = _writes(conn)
    assert len(deletes) == 1
    sql, params = deletes[0]
    assert "trade_date > %s" in sql and ">=" not in sql
    assert params == ("ZZQ", LAST)


def test_apply_is_one_transaction() -> None:
    conn = _Conn(
        tables=["alpha_daily", "beta_daily"],
        evidence={"ZZQ": _retired(), "ZZR": _retired()},
        counts={("alpha_daily", "ZZQ"): 4, ("beta_daily", "ZZR"): 5},
    )
    out = rp.purge(conn, ["ZZQ", "ZZR"], apply=True, today=TODAY)

    assert conn.commits == 1
    assert out["total"] == 9
    assert {s[1][0] for s in _writes(conn)} == {"ZZQ", "ZZR"}


def test_a_failed_delete_rolls_everything_back() -> None:
    conn = _Conn(
        tables=["alpha_daily", "beta_daily"],
        evidence={"ZZQ": _retired()},
        counts={("alpha_daily", "ZZQ"): 4, ("beta_daily", "ZZQ"): 5},
        fail_on="beta_daily",
    )
    with pytest.raises(RuntimeError):
        rp.purge(conn, ["ZZQ"], apply=True, today=TODAY)
    assert conn.commits == 0
    assert conn.rollbacks == 1


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        ((None, None, None), "no_bars"),
        ((LAST, None, None), "no_ticker_row"),
        ((LAST, True, None), "still_active"),
        ((date(2031, 3, 12), False, None), "traded_recently"),
    ],
)
def test_a_listing_is_refused_unless_both_signals_agree(
    evidence: tuple[Any, Any, Any], reason: str
) -> None:
    conn = _Conn(
        tables=["alpha_daily"], evidence={"ZZQ": evidence}, counts={("alpha_daily", "ZZQ"): 6}
    )
    out = rp.purge(conn, ["ZZQ"], apply=True, today=TODAY)

    assert out["symbols"]["ZZQ"]["refused"] == reason
    assert out["total"] == 0
    assert _writes(conn) == []


def test_a_retirement_date_is_not_required() -> None:
    # The vendor's retirement date is filled a few names per run, so one of the
    # two measured listings had none yet. Inactive plus stale bars is enough.
    assert "refused" not in rp.retirement(
        _Conn(tables=[], evidence={"ZZQ": _retired()}), "ZZQ", today=TODAY
    )


def test_only_features_is_written() -> None:
    conn = _Conn(
        tables=["alpha_daily"], evidence={"ZZQ": _retired()}, counts={("alpha_daily", "ZZQ"): 1}
    )
    rp.purge(conn, ["ZZQ"], apply=True, today=TODAY)

    assert _writes(conn)
    assert all(sql.startswith("DELETE FROM features.") for sql, _ in _writes(conn))


def test_a_catalog_name_that_is_not_a_plain_identifier_stops_the_run() -> None:
    conn = _Conn(tables=["alpha_daily", 'beta"; DROP TABLE x; --'], evidence={"ZZQ": _retired()})
    with pytest.raises(ValueError):
        rp.purge(conn, ["ZZQ"], apply=False, today=TODAY)
    assert _writes(conn) == []


def test_the_curated_list_is_the_two_measured_listings() -> None:
    assert rp.RETIRED == ("AVB", "WBS")
