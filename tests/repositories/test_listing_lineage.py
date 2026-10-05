"""B7 — a listing's stock history across a rename, and where a delisting ends it."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from bifrost_research.repositories import listing_lineage as ll


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn
        self.rows: list[Any] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        q = " ".join(sql.split()).lower()
        self.conn.seen.append((q, params))
        bars = self.conn.bars
        if "alive_first" in q:  # rename_history_move.handover
            dead, alive, _dead = params
            dead_last = max(d for s, d, _c in bars if s == dead)
            alive_first = min(d for s, d, _c in bars if s == alive and d > dead_last)
            self.rows = [(dead_last, alive_first)]
        elif "from raw_market.ticker" in q:
            sym, _ = params
            last = max((d for s, d, _c in bars if s == sym), default=None)
            self.rows = [(last, sym not in self.conn.inactive)]
        elif "from raw_market.stock_daily" in q:
            *sym_params, as_of, limit = params
            if len(sym_params) == 1:
                keep = [(d, c) for s, d, c in bars if s == sym_params[0]]
            else:
                dead, cut, alive, _cut = sym_params
                keep = [(d, c) for s, d, c in bars if (s == dead and d < cut) or (s == alive and d >= cut)]
            self.rows = sorted((d, c) for d, c in keep if d >= as_of)[:limit]
        else:
            self.rows = []

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, bars: list[tuple[str, date, float]], inactive: set[str] | None = None) -> None:
        self.bars = bars
        self.inactive = inactive or set()
        self.seen: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def rollback(self) -> None:
        return None


def _days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    ll._HANDOVERS.clear()


def test_labels_follow_the_rename_both_ways() -> None:
    assert ll.live_label("sats") == "ECHO"
    assert ll.live_label("ECHO") == "ECHO"
    assert ll.labels("SATS") == ["SATS", "ECHO"]
    assert ll.labels("NVDA") == ["NVDA"]


def test_a_never_renamed_name_keeps_the_plain_predicate() -> None:
    clause, params = ll.stock_clause(_Conn([]), "nvda")
    assert clause == "symbol = %s" and params == ["NVDA"]


def test_forward_window_reads_across_the_handover() -> None:
    days = _days(date(2026, 6, 15), 12)
    cut = date(2026, 6, 24)
    # ECHO is a reused ticker: an old 2021 bar must not join the series.
    bars = [("ECHO", date(2021, 11, 1), 1.0)]
    bars += [("SATS", d, 100.0) for d in days if d < cut]
    bars += [("ECHO", d, 110.0) for d in days if d >= cut]
    conn = _Conn(bars)
    leg = ll.forward_leg(conn, "SATS", days[0], 8, today=date(2026, 10, 1))
    assert leg is not None and not leg.delisted
    assert leg.entry_close == 100.0 and leg.exit_close == 110.0
    assert leg.ret == pytest.approx(0.10)


def test_delisted_inside_the_window_settles_at_the_last_close() -> None:
    days = _days(date(2026, 8, 10), 5)  # last close 2026-08-14
    bars = [("AVB", d, 200.0) for d in days[:-1]] + [("AVB", days[-1], 180.0)]
    conn = _Conn(bars, inactive={"AVB"})
    leg = ll.forward_leg(conn, "AVB", days[0], 20, today=date(2026, 10, 1))
    assert leg is not None and leg.delisted
    assert leg.exit_date == days[-1]
    assert leg.ret == pytest.approx(-0.10)


def test_a_window_still_open_stays_unknown() -> None:
    days = _days(date(2026, 9, 21), 5)
    conn = _Conn([("NVDA", d, 100.0) for d in days])
    assert ll.forward_leg(conn, "NVDA", days[0], 20, today=date(2026, 10, 1)) is None


def test_an_inactive_flag_alone_is_not_a_delisting() -> None:
    # Closes inside LIVENESS_DAYS of today: a lagging flag or a halt, not an end.
    days = _days(date(2026, 9, 21), 5)
    conn = _Conn([("XYZ", d, 100.0) for d in days], inactive={"XYZ"})
    assert ll.listing_end(conn, "XYZ", as_of=date(2026, 10, 1)) is None
    assert ll.forward_leg(conn, "XYZ", days[0], 20, today=date(2026, 10, 1)) is None
