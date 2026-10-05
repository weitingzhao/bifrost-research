"""A listing's stock history across a ticker change, and where it ends.

Two ways a name drops out of a forward window, and both made every evaluation
look better than it was (backtest review B7, 2026-10-04):

**Renamed.** SATS → ECHO, ISSC → IA and EQR → VMRK changed ticker without
changing company. Plugin 0.51.0 moved the pre-rename option history onto the
live symbol, so the option axis is continuous; ``raw_market.stock_daily`` still
keeps each label's bars apart. A window that crossed the handover found no
closes after it under the old label, or none before it under the new one, and
the event was skipped. ECHO is also a reused ticker (Echo Global Logistics,
2021), so the new label's bars only count from the handover on.

**Delisted.** AVB (merged into VMRK, last close 2026-08-14) and WBS (acquired by
Santander, 2026-08-19) stopped trading. A window that ran past the last close
returned None and the event was left out of every hit rate and backtest — the
names that went away were the ones that never counted. The feed carries no
delisting return, so the last close stands in for it: a cash or stock takeover
trades at about the consideration by then, and a failure trades at about what
holders got. The window is closed at that close and the reader is told.

A name counts as retired only by ``repositories.listing_status``'s rule: its
``raw_market.ticker`` row says inactive and it has no close within
``LIVENESS_DAYS`` of the day asking. A halt or a holiday week is not a delisting.

Read-only: ``raw_market.stock_daily``, ``raw_market.ticker``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from bifrost_research.engines.rename_history_move import RENAMES, handover
from bifrost_research.repositories.listing_status import liveness_floor

logger = logging.getLogger(__name__)

_SUCCESSOR: dict[str, str] = {dead: alive for dead, alive in RENAMES}
_PREDECESSOR: dict[str, str] = {alive: dead for dead, alive in RENAMES}

# A handover is a fact about the past; it does not move once it has happened.
_HANDOVERS: dict[tuple[str, str], date] = {}


def live_label(symbol: str) -> str:
    """The symbol the company trades under now (itself when never renamed)."""
    sym = str(symbol or "").strip().upper()
    seen = {sym}
    while sym in _SUCCESSOR:
        sym = _SUCCESSOR[sym]
        if sym in seen:  # pragma: no cover - a cycle would be a bad RENAMES edit
            break
        seen.add(sym)
    return sym


def labels(symbol: str) -> list[str]:
    """Every ticker the company has carried in this universe, oldest first."""
    sym = live_label(symbol)
    out = [sym]
    while out[0] in _PREDECESSOR and _PREDECESSOR[out[0]] not in out:
        out.insert(0, _PREDECESSOR[out[0]])
    return out


def _rollback(conn: Any) -> None:
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        try:
            rollback()
        except Exception:  # noqa: BLE001, S110
            pass


def _handover(conn: Any, dead: str, alive: str) -> date | None:
    key = (dead, alive)
    if key not in _HANDOVERS:
        try:
            cut = handover(conn, dead, alive)
        except Exception as exc:  # noqa: BLE001 — a missing handover reads one label
            logger.debug("handover lookup failed for %s -> %s: %s", dead, alive, exc)
            _rollback(conn)
            return None
        if isinstance(cut, datetime):
            cut = cut.date()
        if cut is None:
            return None
        _HANDOVERS[key] = cut
    return _HANDOVERS[key]


def stock_clause(
    conn: Any, symbol: str, *, symbol_col: str = "symbol", date_col: str = "bar_date"
) -> tuple[str, list[Any]]:
    """A ``WHERE`` fragment selecting the company's stock bars across a rename.

    ``symbol_col`` / ``date_col`` are column references from the caller's own
    query, not input. A name that never changed ticker gets the plain
    ``symbol = %s`` with the live label, so its query is unchanged.
    """
    sym = live_label(symbol)
    dead = _PREDECESSOR.get(sym)
    cut = _handover(conn, dead, sym) if dead else None
    if dead is None or cut is None:
        return f"{symbol_col} = %s", [sym]
    return (
        f"(({symbol_col} = %s AND {date_col} < %s) OR ({symbol_col} = %s AND {date_col} >= %s))",
        [dead, cut, sym, cut],
    )


def listing_end(conn: Any, symbol: str, *, as_of: date) -> date | None:
    """The last close of a retired listing, or None while it still trades."""
    sym = live_label(symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT (SELECT max(bar_date) FROM raw_market.stock_daily
                         WHERE symbol = %s AND close > 0) AS last_bar,
                       (SELECT t.active FROM raw_market.ticker AS t
                         WHERE t.symbol = %s LIMIT 1) AS active
                """,
                (sym, sym),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — unknown reads as still trading
        logger.debug("listing_end lookup failed for %s: %s", sym, exc)
        _rollback(conn)
        return None
    if not row:
        return None
    last_bar, active = (row.get("last_bar"), row.get("active")) if isinstance(row, Mapping) else (row[0], row[1])
    if isinstance(last_bar, datetime):
        last_bar = last_bar.date()
    if active is not False or not isinstance(last_bar, date):
        return None
    if last_bar > liveness_floor(as_of):
        return None
    return last_bar


@dataclass(frozen=True)
class ForwardLeg:
    """Entry and exit closes of a forward window, in sessions.

    ``delisted`` is set when the listing stopped trading inside the window: the
    exit is then its last close and ``exit_date`` that session, short of the
    horizon.
    """

    entry_date: date
    entry_close: float
    exit_date: date
    exit_close: float
    delisted: bool = False

    @property
    def ret(self) -> float:
        return (self.exit_close / self.entry_close) - 1.0


def forward_leg(conn: Any, symbol: str, as_of: date, horizon: int, *, today: date) -> ForwardLeg | None:
    """The close on the first session at or after ``as_of`` and ``horizon`` sessions on.

    None when the window has not elapsed yet (or the name has no bars there).
    A window cut short by a delisting closes at the last close rather than
    returning None; a window cut short by a rename reads on under the new label.
    """
    clause, params = stock_clause(conn, symbol)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT bar_date, close::float
            FROM raw_market.stock_daily
            WHERE {clause}
              AND bar_date >= %s
              AND close IS NOT NULL AND close > 0
            ORDER BY bar_date ASC
            LIMIT %s
            """,
            (*params, as_of, horizon + 1),
        )
        rows = cur.fetchall() or []
    if not rows or float(rows[0][1]) <= 0:
        return None
    if len(rows) >= horizon + 1:
        return ForwardLeg(rows[0][0], float(rows[0][1]), rows[horizon][0], float(rows[horizon][1]))
    end = listing_end(conn, symbol, as_of=today)
    if end is None or rows[-1][0] != end:
        return None
    return ForwardLeg(rows[0][0], float(rows[0][1]), rows[-1][0], float(rows[-1][1]), delisted=True)


__all__ = [
    "ForwardLeg",
    "forward_leg",
    "labels",
    "listing_end",
    "live_label",
    "stock_clause",
]
