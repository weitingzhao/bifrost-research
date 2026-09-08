"""Repair must be driven by what is missing, not by re-deriving what should exist.

`signal_hit` writes one row per lens trigger per day. Re-walking a past day
rebuilds its rows from today's view of that date, so a lens whose inputs have
since moved produces no row for it, the upsert never reaches the old row, and a
NULL forward return stands however wide the window gets. On 2026-09-08 nineteen
rows from 2026-08-03..08-07 sat unjudged for exactly that reason while the same
run rewrote 198 of their neighbours — ANET and SPCX on 08-03 had all 21 bars
they needed by then.
"""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.signal_hit import entry


class _Cur:
    def __init__(self, outer):
        self.outer = outer

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.outer.statements.append((" ".join(str(sql).split()), params))

    def fetchall(self):
        return self.outer.pending if "SELECT trade_date" in self.outer.statements[-1][0] else []


class _Conn:
    def __init__(self, pending):
        self.pending = pending
        self.statements: list[tuple[str, object]] = []
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1


def test_it_selects_rows_by_missing_forward_return(monkeypatch):
    conn = _Conn([(date(2026, 8, 3), "ANET", "iv_rank", "cold", True, True)])
    monkeypatch.setattr(entry, "_fwd_return", lambda *a, **k: 0.05)
    monkeypatch.setattr(entry, "hit_for", lambda *a, **k: True)

    out = entry.backfill_missing_forward(conn, horizons=(5, 20))

    select = conn.statements[0][0]
    assert "fwd_return_5d IS NULL OR fwd_return_20d IS NULL" in select
    # Not a time window: a repair horizon shorter than the damage is the defect.
    assert "trade_date >=" not in select
    assert out["filled"] == {"5d": 1, "20d": 1}
    assert conn.commits == 1


def test_a_window_that_has_not_elapsed_stays_unknown(monkeypatch):
    """An unknown outcome must never be written as a miss."""
    conn = _Conn([(date(2026, 9, 4), "AAPL", "iv_rank", "cold", True, True)])
    monkeypatch.setattr(entry, "_fwd_return", lambda *a, **k: None)

    out = entry.backfill_missing_forward(conn, horizons=(5, 20))

    assert out["filled"] == {"5d": 0, "20d": 0}
    assert conn.commits == 0, "nothing to write means nothing is written"
    assert not any("UPDATE" in s for s, _ in conn.statements)


def test_it_only_fills_the_horizon_that_became_computable(monkeypatch):
    conn = _Conn([(date(2026, 8, 3), "ANET", "iv_rank", "cold", True, True)])
    monkeypatch.setattr(entry, "_fwd_return", lambda c, s, d, h: 0.05 if h == 5 else None)
    monkeypatch.setattr(entry, "hit_for", lambda *a, **k: True)

    out = entry.backfill_missing_forward(conn, horizons=(5, 20))

    assert out["filled"] == {"5d": 1, "20d": 0}
    update = [s for s, _ in conn.statements if "UPDATE" in s][0]
    assert "fwd_return_5d = %s" in update
    assert "fwd_return_20d" not in update


def test_a_horizon_that_is_already_filled_is_not_rewritten(monkeypatch):
    """Selecting a row for its blank 20d must not rewrite its good 5d.

    The first run of this pass reported 805 fills for 5d where eleven rows had
    actually been repaired — every row picked up for a missing 20d had its
    already-correct 5d recomputed and written back.
    """
    # 5d is present (False), 20d is blank (True).
    conn = _Conn([(date(2026, 8, 3), "ANET", "iv_rank", "cold", False, True)])
    monkeypatch.setattr(entry, "_fwd_return", lambda *a, **k: 0.05)
    monkeypatch.setattr(entry, "hit_for", lambda *a, **k: True)

    out = entry.backfill_missing_forward(conn, horizons=(5, 20))

    assert out["filled"] == {"5d": 0, "20d": 1}
    update = [s for s, _ in conn.statements if "UPDATE" in s][0]
    assert "fwd_return_20d = %s" in update
    assert "fwd_return_5d" not in update, "a column that was not blank must not be touched"
