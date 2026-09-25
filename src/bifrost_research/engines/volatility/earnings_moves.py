"""Earnings moves: what the straddle priced before each print, and what came.

Print dates are the name's 8-K filings carrying Item 2.02 (results of operations),
from ``raw_market.sec_8k_filing``. A filing carries a date, not a time, so the
release came before the open (the filing day's session is the print) or after the
close (the next session is). The window runs from the last session before the
filing date to the first session after it, and the print is whichever of its
sessions moved more — a change over both would fold an ordinary day into it
(NVDA filed 2025-02-26: 126.63 → 131.28 → 120.15, a −8.5% print that the
two-session change would read as −5.1%).

- actual  the larger single-session move in the window, with its sign kept as
          ``direction`` (a weekend filing has one session: before → after)
- priced  the at-the-money straddle for the first expiry on or after the window's
          close, priced by Black–Scholes at that expiry's ATM IV as of the session
          before, divided by that session's close — the move the market charged
- crush   ATM IV after the window less before, in vol points, for the first expiry
          both sessions price (the front weekly often has under five days left after
          the print, where the store has no Brent IV); ``crush_expiry`` names it

IVs come from ``features.option_metric_atm_iv_daily`` (the repaired store: vendor
snapshots where observed, Brent from option_daily elsewhere).
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median
from typing import Any, Sequence

from bifrost_research.engines.backtest.canonical_pnl import bs_price

# 8-K/A amendments and follow-up filings land within days of the release.
SAME_PRINT_DAYS = 7


def print_dates(filing_dates: Sequence[date], *, limit: int) -> list[date]:
    """The newest ``limit`` prints, oldest first; filings within ``SAME_PRINT_DAYS``
    of a kept one are the same print."""
    kept: list[date] = []
    for d in sorted(set(filing_dates)):
        if kept and (d - kept[-1]).days <= SAME_PRINT_DAYS:
            continue
        kept.append(d)
    return kept[-limit:] if limit > 0 else kept


def straddle_move(spot: float, strike: float, iv: float, days: int) -> float | None:
    """ATM straddle as a fraction of spot."""
    if spot <= 0 or strike <= 0 or iv <= 0 or days <= 0:
        return None
    t = days / 365.0
    return (bs_price(spot, strike, t, iv, right="C") + bs_price(spot, strike, t, iv, right="P")) / spot


def one_print(
    d: date,
    closes: dict[date, float],
    atm: dict[date, list[tuple[date, float, float]]],
) -> dict[str, Any]:
    """The row for a print filed on ``d``. ``atm`` maps a session to its
    (expiry, atm_strike, atm_iv) rows."""
    sessions = sorted(closes)
    before = next((s for s in reversed(sessions) if s < d), None)
    after = next((s for s in sessions if s > d), None)
    row: dict[str, Any] = {
        "filed": d.isoformat(),
        "before": before.isoformat() if before else None,
        "after": after.isoformat() if after else None,
        "actual": None,
        "direction": None,
        "priced": None,
        "ratio": None,
        "crush_pts": None,
        "expiry": None,
        "crush_expiry": None,
        "missing": None,
    }
    if before is None or after is None:
        row["missing"] = "no close after the print yet" if before else "no close before the print"
        return row
    if d in closes:
        move = max((closes[d] / closes[before] - 1.0, closes[after] / closes[d] - 1.0), key=abs)
    else:
        move = closes[after] / closes[before] - 1.0
    row["actual"] = round(abs(move), 6)
    row["direction"] = "up" if move > 0 else "down" if move < 0 else "flat"

    front = sorted((e, k, iv) for e, k, iv in atm.get(before, []) if e >= after)
    if not front:
        row["missing"] = "no ATM IV for an expiry covering the print"
        return row
    expiry, strike, iv = front[0]
    priced = straddle_move(closes[before], strike, iv, (expiry - before).days)
    row["expiry"] = expiry.isoformat()
    if priced:
        row["priced"] = round(priced, 6)
        row["ratio"] = round(abs(move) / priced, 4)
    ivs_before = {e: v for e, _k, v in atm.get(before, []) if e >= after}
    ivs_after = {e: v for e, _k, v in atm.get(after, [])}
    common = sorted(set(ivs_before) & set(ivs_after))
    if common:
        e = common[0]
        row["crush_pts"] = round((ivs_after[e] - ivs_before[e]) * 100.0, 2)
        row["crush_expiry"] = e.isoformat()
    return row


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ratios = [r["ratio"] for r in rows if r.get("ratio") is not None]
    return {
        "n": len(ratios),
        "median_ratio": round(median(ratios), 4) if ratios else None,
        # The straddle was rich when the move came in under what it charged.
        "rich": sum(1 for x in ratios if x < 1.0),
    }


def earnings_moves(conn: Any, symbol: str, *, limit: int = 8, as_of: date | None = None) -> dict[str, Any]:
    sym = symbol.strip().upper()
    today = as_of or date.today()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT filing_date,
                   bool_or('2.02' = ANY(items))
            FROM raw_market.sec_8k_filing
            WHERE symbol = %s AND filing_date <= %s
            GROUP BY filing_date
            """,
            (sym, today),
        )
        filed = cur.fetchall() or []
    filings = len(filed)
    prints = print_dates([d for d, is_print in filed if is_print], limit=limit)
    out: dict[str, Any] = {"symbol": sym, "filing_days": filings, "prints": []}
    if not prints:
        return {**out, **summarize([])}

    lo, hi = prints[0] - timedelta(days=10), prints[-1] + timedelta(days=10)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bar_date, close FROM raw_market.stock_daily
            WHERE symbol = %s AND bar_date BETWEEN %s AND %s AND close > 0
            """,
            (sym, lo, hi),
        )
        closes = {d: float(c) for d, c in (cur.fetchall() or [])}
        sessions = sorted(closes)
        wanted: set[date] = set()
        for d in prints:
            wanted.update(s for s in sessions if s < d and s >= d - timedelta(days=10))
            wanted.update(s for s in sessions if s > d and s <= d + timedelta(days=10))
        cur.execute(
            """
            SELECT trade_date, expiry, atm_strike, atm_iv
            FROM features.option_metric_atm_iv_daily
            WHERE symbol = %s AND trade_date = ANY(%s) AND atm_iv > 0
            """,
            (sym, sorted(wanted)),
        )
        atm: dict[date, list[tuple[date, float, float]]] = {}
        for td, exp, k, iv in cur.fetchall() or []:
            atm.setdefault(td, []).append((exp, float(k), float(iv)))

    rows = [one_print(d, closes, atm) for d in prints]
    rows.reverse()  # newest first, as the panel reads
    return {**out, "prints": rows, **summarize(rows)}


__all__ = ["earnings_moves", "one_print", "print_dates", "straddle_move", "summarize", "SAME_PRINT_DAYS"]
