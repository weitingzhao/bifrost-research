"""Recompute the sessions whose IV features cover far fewer names than raw (2026-09-28).

The volatility slots recompute three sessions and the Sunday backfill ninety, so a
session the plugin fills later than that keeps the ATM IV computed from the thin raw
it had. Measured 2026-09-28: 2025-03-12 carried 487 names against 588 in raw with a
contract inside the solver's window; recomputed, 546. 2026-08-26 carried 315 against
652.

Coverage per session is universe names with ATM IV over universe names raw holds a
contract for with ``DTE_MIN..DTE_MAX`` days to expiry. ATM IV's own filters drop a
few (0.93 on 2025-03-12 once recomputed), so a session below ``COVERAGE_RATIO`` is
stale, not selective. It is recomputed only when raw was written after its features
were: one that stays short once recomputed from the raw it has is what raw supports,
and recomputing it weekly would rerun every session after it for nothing.

A short session gets ATM IV and PCR again. IV percentile and VRP then run on every
session from the first short one on, oldest first: each ranks against the stored
values of the sessions before it.

Usage::

    python -m bifrost_research.engines.volatility.iv_coverage_heal          # counts only
    python -m bifrost_research.engines.volatility.iv_coverage_heal --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import load_symbols_from_env_or_query, union_iv_radar_benchmarks
from bifrost_research.db.conn import connect
from bifrost_research.engines.adjusted_contracts import not_adjusted_contract_sql
from bifrost_research.engines.volatility.atm_iv import compute_atm_iv_for_date
from bifrost_research.engines.volatility.iv_history_repair import sessions
from bifrost_research.engines.volatility.iv_percentile import compute_iv_percentile_for_date
from bifrost_research.engines.volatility.iv_solver import DTE_MAX, DTE_MIN
from bifrost_research.engines.volatility.pcr import compute_pcr_for_date
from bifrost_research.engines.vrp.compute import compute_vrp_for_date

logger = logging.getLogger(__name__)

COVERAGE_WINDOW_DAYS = 730  # Options Starter: two rolling years of option_daily
COVERAGE_RATIO = 0.85


def months(start: date, end: date) -> Iterator[tuple[date, date]]:
    """Calendar months covering ``[start, end]``, each clipped to the range."""
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur + timedelta(days=32)).replace(day=1)
        yield max(cur, start), min(nxt - timedelta(days=1), end)
        cur = nxt


Breadth = dict[date, tuple[int, datetime | None]]  # names, last write


def raw_breadth(conn: Any, universe: Sequence[str], start: date, end: date) -> Breadth:
    """Universe names per session with an option bar ``DTE_MIN..DTE_MAX`` days from
    expiry, and when that session's bars were last fetched.

    One statement per month with the bounds as literals: option_daily is partitioned
    monthly and a parameter bound scans every partition (35s against 0.2s a month).
    Adjusted contracts do not count: ATM IV leaves them out, so a name holding only
    those is not coverage the features could have.
    """
    out: Breadth = {}
    for lo, hi in months(start, end):
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120s'")
            cur.execute(
                f"""
                SELECT bar_date, count(DISTINCT underlying)::bigint, max(fetched_at)
                FROM raw_market.option_daily
                WHERE bar_date >= DATE '{lo.isoformat()}' AND bar_date <= DATE '{hi.isoformat()}'
                  AND underlying = ANY(%s)
                  AND (expiry - bar_date) BETWEEN %s AND %s
                  AND {not_adjusted_contract_sql("option_ticker")}
                GROUP BY 1
                """,
                (list(universe), DTE_MIN, DTE_MAX),
            )
            out.update({r[0]: (int(r[1]), r[2]) for r in cur.fetchall()})
        conn.commit()
    return out


def feature_breadth(conn: Any, universe: Sequence[str], start: date, end: date) -> Breadth:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, count(DISTINCT symbol)::bigint, max(computed_at)
            FROM features.option_metric_atm_iv_daily
            WHERE trade_date BETWEEN %s AND %s AND symbol = ANY(%s)
            GROUP BY 1
            """,
            (start, end, list(universe)),
        )
        out = {r[0]: (int(r[1]), r[2]) for r in cur.fetchall()}
    conn.commit()
    return out


def short_sessions(
    days: Sequence[date],
    raw: Breadth,
    features: Breadth,
    *,
    ratio: float = COVERAGE_RATIO,
) -> list[tuple[date, int, int]]:
    """``(session, feature names, raw names)`` where features fall below ``ratio`` of
    raw and raw was written after them."""
    out = []
    for d in days:
        r, fetched = raw.get(d, (0, None))
        f, computed = features.get(d, (0, None))
        if not r or f >= ratio * r:
            continue
        if computed is None or fetched is None or fetched > computed:
            out.append((d, f, r))
    return out


def heal(
    conn: Any,
    short: Sequence[date],
    days: Sequence[date],
    universe: Sequence[str],
    *,
    progress: Callable[[date], None] | None = None,
) -> dict[str, int]:
    """ATM IV and PCR on the short sessions; IV percentile and VRP on every session
    from the first of them on, oldest first."""
    todo = set(short)
    counts = {"atm_rows": 0, "pcr_rows": 0, "pct_rows": 0, "vrp_rows": 0, "sessions": 0}
    if not todo:
        return counts
    first = min(todo)
    for td in sorted(d for d in days if d >= first):
        if td in todo:
            counts["atm_rows"] += int(compute_atm_iv_for_date(conn, trade_date=td, underlyings=universe).get("rows_written") or 0)
            counts["pcr_rows"] += int(compute_pcr_for_date(conn, trade_date=td, underlyings=universe).get("rows_written") or 0)
        counts["pct_rows"] += int(compute_iv_percentile_for_date(conn, trade_date=td, underlyings=universe).get("rows_written") or 0)
        counts["vrp_rows"] += int(compute_vrp_for_date(conn, trade_date=td, underlyings=universe).get("rows_written") or 0)
        conn.commit()
        counts["sessions"] += 1
        if progress:
            progress(td)
    return counts


def run(
    *,
    end: date | None = None,
    window_days: int = COVERAGE_WINDOW_DAYS,
    ratio: float = COVERAGE_RATIO,
    apply: bool = True,
) -> dict[str, Any]:
    end = end or datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    start = end - timedelta(days=int(window_days))
    conn = connect()
    try:
        universe = union_iv_radar_benchmarks(load_symbols_from_env_or_query(conn))
        days = sessions(conn, start, end)
        raw = raw_breadth(conn, universe, start, end)
        short = short_sessions(days, raw, feature_breadth(conn, universe, start, end), ratio=ratio)
        result: dict[str, Any] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "universe": len(universe),
            "sessions": len(days),
            "short": len(short),
            "short_sample": [{"date": d.isoformat(), "features": f, "raw": r} for d, f, r in short[:20]],
            "applied": apply,
        }
        if apply and short:
            result.update(
                heal(
                    conn,
                    [d for d, _f, _r in short],
                    days,
                    universe,
                    progress=lambda d: logger.info("iv coverage heal %s", d),
                )
            )
        logger.info("iv coverage heal short=%s applied=%s", len(short), apply)
        return result
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recompute IV features on sessions short of raw coverage")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (default: today in New York)")
    parser.add_argument("--window-days", type=int, default=COVERAGE_WINDOW_DAYS)
    parser.add_argument("--ratio", type=float, default=COVERAGE_RATIO)
    parser.add_argument("--apply", action="store_true", help="recompute; without it only counts")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run(
        end=date.fromisoformat(args.end) if args.end else None,
        window_days=args.window_days,
        ratio=args.ratio,
        apply=args.apply,
    )
    print(json.dumps(result, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
