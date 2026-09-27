"""Moving a renamed company's derived history: what gets relabelled, what gets purged.

The two halves need opposite treatment and the boundary between them is a date
read from the data, so these pin the boundary, the refusal, and the direction.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from bifrost_research.engines import rename_history_move as mv

CUT = date(2026, 6, 24)


class _Cur:
    def __init__(self, parent: _Conn) -> None:
        self.parent = parent
        self.rowcount = 0
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        q = " ".join(str(sql).split())
        self.parent.statements.append((q, params))
        if "min(bar_date)" in q:
            self._rows = [self.parent.handover]
            return
        if q.startswith("SELECT DISTINCT bar_date"):
            self._rows = [(d,) for d in self.parent.sessions]
            return
        if q.startswith("SELECT count(*)"):
            table = q.split("FROM features.")[1].split(" ")[0]
            symbol = params[0]
            before = "trade_date <" in q
            n = self.parent.counts.get((table, symbol, before), 0)
            if "NOT EXISTS" in q:
                n -= self.parent.collisions.get((table, symbol), 0)
            self._rows = [(n,)]
            return
        if q.startswith("UPDATE") or q.startswith("DELETE"):
            table = q.split("features.")[1].split(" ")[0]
            symbol = params[1] if q.startswith("UPDATE") else params[0]
            before = "trade_date <" in q
            n = self.parent.counts.get((table, symbol, before), 0)
            if "NOT EXISTS" in q:
                n -= self.parent.collisions.get((table, symbol), 0)
            self.rowcount = n
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
        counts: dict[tuple[str, str, bool], int] | None = None,
        *,
        handover: tuple[Any, Any] = (date(2026, 6, 23), CUT),
        sessions: list[date] | None = None,
        collisions: dict[tuple[str, str], int] | None = None,
    ) -> None:
        self.counts = counts or {}
        self.collisions = collisions or {}
        self.handover = handover
        self.sessions = sessions or []
        self.statements: list[tuple[str, Any]] = []
        self.commits = 0

    def cursor(self) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        return None


def test_the_handover_is_the_successor_first_close_after_the_old_one_stopped() -> None:
    """ECHO is a reused ticker: Echo Global Logistics held it in 2021. An
    unconditional min(bar_date) puts the handover 4.6 years early."""
    conn = _Conn()
    assert mv.handover(conn, "SATS", "ECHO") == CUT
    sql = " ".join(conn.statements[0][0].split())
    assert "bar_date > (SELECT max(bar_date)" in sql, "the successor's first close is filtered by the old one's last"


def test_a_pair_with_no_successor_close_is_skipped_not_guessed() -> None:
    conn = _Conn(handover=(date(2026, 6, 23), None))
    assert mv.handover(conn, "SATS", "ECHO") is None


def test_rows_before_the_handover_are_relabelled_not_recomputed() -> None:
    """No ECHO-rooted contract exists for those dates, so a recompute would
    write nothing and the history would be lost."""
    conn = _Conn({("option_metric_atm_iv_daily", "SATS", True): 1264})
    out = mv.relabel(conn, apply=True)
    assert out["rows"]["option_metric_atm_iv_daily:SATS->ECHO"] == 1264
    update = next(q for q, _ in conn.statements if q.startswith("UPDATE"))
    assert "SET symbol = %s" in update and "trade_date < %s" in update


def test_relabel_leaves_behind_only_the_sessions_the_live_symbol_already_has() -> None:
    """Measured: scan_daily holds twelve rows for both SATS and ECHO on the same
    twelve sessions. Both cannot survive the primary key and choosing between
    them invents history, so those sessions stay and the rest still move."""
    conn = _Conn(
        {("stock_signal_scan_daily", "SATS", True): 12},
        collisions={("stock_signal_scan_daily", "SATS"): 12},
    )
    out = mv.relabel(conn, apply=True)
    assert out["rows"] == {}, "every session collided, so nothing moved"
    assert out["left_behind"] and "stock_signal_scan_daily:SATS->ECHO" in out["left_behind"][0]
    assert not [q for q, _ in conn.statements if q.startswith("UPDATE")]


def test_a_partial_collision_moves_the_rest_and_says_what_stayed() -> None:
    conn = _Conn(
        {("stock_signal_vrp_daily", "SATS", True): 448},
        collisions={("stock_signal_vrp_daily", "SATS"): 3},
    )
    out = mv.relabel(conn, apply=True)
    assert out["rows"]["stock_signal_vrp_daily:SATS->ECHO"] == 445
    assert "3 row(s)" in out["left_behind"][0]
    update = next(q for q, _ in conn.statements if q.startswith("UPDATE"))
    assert "NOT EXISTS" in update, "the session guard has to be in the write, not only the count"


def test_rows_on_or_after_the_handover_are_purged() -> None:
    """They were computed from the successor's contracts under the wrong name;
    the recompute counts the same contracts once, under the live symbol."""
    conn = _Conn({("option_metric_max_pain_daily", "SATS", False): 156})
    out = mv.purge(conn, apply=True)
    assert out["rows"]["option_metric_max_pain_daily:SATS"] == 156
    delete = next(q for q, _ in conn.statements if q.startswith("DELETE"))
    assert "trade_date >= %s" in delete


def test_a_dry_run_counts_and_writes_nothing() -> None:
    conn = _Conn(
        {
            ("option_metric_atm_iv_daily", "SATS", True): 1264,
            ("option_metric_max_pain_daily", "SATS", False): 156,
        }
    )
    rl = mv.relabel(conn, apply=False)
    pg = mv.purge(conn, apply=False)
    assert rl["total"] == 1264 and pg["total"] == 156
    assert rl["applied"] is False and pg["applied"] is False
    assert not [q for q, _ in conn.statements if q.startswith(("UPDATE", "DELETE"))]
    assert conn.commits == 0


def test_a_dry_run_recompute_reports_the_window_without_calling_an_engine() -> None:
    conn = _Conn(sessions=[date(2026, 6, 24), date(2026, 6, 25)])
    out = mv.recompute(conn, apply=False)
    assert out["sessions"] == 2 and out["applied"] is False
    assert out["first"] == "2026-06-24" and out["last"] == "2026-06-25"
    assert sorted(out["symbols"]) == ["ECHO", "IA", "VMRK"]


def test_every_affected_table_is_visited_for_every_pair() -> None:
    conn = _Conn()
    mv.purge(conn, apply=False)
    counted = {
        q.split("FROM features.")[1].split(" ")[0]
        for q, _ in conn.statements
        if q.startswith("SELECT count(*)")
    }
    assert counted == set(mv.AFFECTED_TABLES)


def test_the_renames_are_the_three_measured_pairs() -> None:
    """A curated list on purpose — SATS has no raw_market.ticker row to derive from."""
    assert mv.RENAMES == (("SATS", "ECHO"), ("ISSC", "IA"), ("EQR", "VMRK"))
