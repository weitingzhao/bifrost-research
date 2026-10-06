"""Analyze Wave M — daily alert scan engine.

Writes features.stock_signal_alert_daily for:
- composite_high: composite_score >= 90 and rank top-5 on as-of date, re-judged on
  the last REJUDGE_SCAN_DATES scan dates (TD-97)
- weight_shift: adaptive lens weight vs 30d mean > 1σ (informational)
- hit_rate_drop: weekly hot-side hit_rate_5d drops >= 8pp vs prior week
"""

from __future__ import annotations

import argparse
import logging
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from bifrost_research.db.calendar import ny_today
from bifrost_research.db.conn import connect
from bifrost_research.db.upsert import batch_upsert
from bifrost_research.lenses.registry import decay_lens_ids
from bifrost_research.schema.schemas import (
    TABLE_STOCK_SIGNAL_ALERT_DAILY,
    TABLE_STOCK_SIGNAL_LENS_HIT_DAILY,
    TABLE_STOCK_SIGNAL_SCAN_DAILY,
)

logger = logging.getLogger(__name__)

LENSES = decay_lens_ids()
UPSERT_COLS = (
    "trade_date",
    "kind",
    "symbol",
    "lens",
    "severity",
    "reason_json",
    "computed_at",
)


def _asof(conn: Any, as_of: date | None) -> date:
    if as_of:
        return as_of
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(trade_date) FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY}")
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    return ny_today()


def _composite_high_alerts(conn: Any, as_of: date) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, composite_score
            FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY}
            WHERE trade_date = %s AND composite_score >= 90
            ORDER BY composite_score DESC NULLS LAST
            LIMIT 5
            """,
            (as_of,),
        )
        rows = cur.fetchall()
    now = datetime.now(timezone.utc)
    out: list[tuple[Any, ...]] = []
    for i, (sym, score) in enumerate(rows, start=1):
        out.append(
            (
                as_of,
                "composite_high",
                str(sym).upper(),
                "",
                "high",
                {"rank": i, "composite_score": float(score) if score is not None else None},
                now,
            )
        )
    return out


def _weekly_hit_rates(conn: Any, lens: str, side: str, as_of: date) -> list[tuple[str, float, int]]:
    """Return [(iso_week, hit_rate_5d, n), ...] for last ~8 weeks."""
    cutoff = as_of - timedelta(days=70)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trade_date, hit_5d
            FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY}
            WHERE lens = %s AND trigger_side = %s
              AND trade_date >= %s AND trade_date <= %s
              AND hit_5d IS NOT NULL
            ORDER BY trade_date ASC
            """,
            (lens, side, cutoff, as_of),
        )
        rows = cur.fetchall()
    by_week: dict[str, list[bool]] = {}
    for td, hit in rows:
        if not isinstance(td, date):
            continue
        iso = td.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        by_week.setdefault(key, []).append(bool(hit))
    out: list[tuple[str, float, int]] = []
    for key in sorted(by_week.keys()):
        vals = by_week[key]
        if not vals:
            continue
        out.append((key, sum(1 for v in vals if v) / len(vals), len(vals)))
    return out


def _hit_rate_drop_alerts(conn: Any, as_of: date) -> list[tuple[Any, ...]]:
    now = datetime.now(timezone.utc)
    out: list[tuple[Any, ...]] = []
    for lens in LENSES:
        weeks = _weekly_hit_rates(conn, lens, "hot", as_of)
        if len(weeks) < 2:
            continue
        prev_w, prev_rate, prev_n = weeks[-2]
        cur_w, cur_rate, cur_n = weeks[-1]
        drop_pp = (prev_rate - cur_rate) * 100.0
        if drop_pp >= 8.0 and cur_n >= 3 and prev_n >= 3:
            out.append(
                (
                    as_of,
                    "hit_rate_drop",
                    "",
                    lens,
                    "warn",
                    {
                        "side": "hot",
                        "prev_week": prev_w,
                        "curr_week": cur_w,
                        "prev_rate": round(prev_rate, 4),
                        "curr_rate": round(cur_rate, 4),
                        "drop_pp": round(drop_pp, 2),
                        "prev_n": prev_n,
                        "curr_n": cur_n,
                    },
                    now,
                )
            )
    return out


def _adaptive_weight_shift_alerts(conn: Any, as_of: date) -> list[tuple[Any, ...]]:
    """Compare latest 30d hot hit_rate per lens vs mean of rolling 30d windows over ~90d."""
    now = datetime.now(timezone.utc)
    out: list[tuple[Any, ...]] = []
    cutoff = as_of - timedelta(days=120)
    for lens in LENSES:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT trade_date, hit_5d
                FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY}
                WHERE lens = %s AND trigger_side = 'hot'
                  AND trade_date >= %s AND trade_date <= %s
                  AND hit_5d IS NOT NULL
                ORDER BY trade_date ASC
                """,
                (lens, cutoff, as_of),
            )
            rows = [(td, bool(hit)) for td, hit in cur.fetchall() if isinstance(td, date)]
        if len(rows) < 20:
            continue
        rates: list[float] = []
        cursor = as_of
        while cursor >= as_of - timedelta(days=90):
            win_start = cursor - timedelta(days=30)
            subset = [h for td, h in rows if win_start <= td <= cursor]
            if len(subset) >= 5:
                rates.append(sum(1 for h in subset if h) / len(subset))
            cursor -= timedelta(days=7)
        if len(rates) < 4:
            continue
        latest = rates[0]
        hist = rates[1:]
        mu = statistics.mean(hist)
        sigma = statistics.pstdev(hist) if len(hist) > 1 else 0.0
        if sigma <= 0:
            continue
        z = (latest - mu) / sigma
        if abs(z) >= 1.0:
            out.append(
                (
                    as_of,
                    "weight_shift",
                    "",
                    lens,
                    "info" if abs(z) < 1.5 else "warn",
                    {
                        "latest_hit_rate_30d": round(latest, 4),
                        "mean_hit_rate": round(mu, 4),
                        "sigma": round(sigma, 4),
                        "z": round(z, 3),
                    },
                    now,
                )
            )
    return out


#: Scan dates re-judged every run. The scan engine re-walks its last 3 sessions
#: (runners.run_scan), and every composite_score >= 90 row so far appeared on such
#: a recompute (META 08-31 written 09-03; six names of 09-24 written 09-29), after
#: a judge that read each date once had already passed it: composite_high never
#: fired (TD-97). Five covers the re-walk plus a missed night.
REJUDGE_SCAN_DATES = 5


def _scan_dates(conn: Any, newest: date, n: int) -> list[date]:
    """The newest ``n`` scan dates at or before ``newest``, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT trade_date FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY}
            WHERE trade_date <= %s
            ORDER BY trade_date DESC
            LIMIT %s
            """,
            (newest, n),
        )
        rows = cur.fetchall() or []
    days = [r[0] if not isinstance(r, dict) else next(iter(r.values())) for r in rows]
    return sorted(d for d in days if isinstance(d, date)) or [newest]


def _replace(conn: Any, day: date, kind: str, alerts: list[tuple[Any, ...]]) -> int:
    """Replace ``kind``'s alerts on ``day`` in one transaction; returns rows removed.

    A recompute can raise an alert or take one back: an upsert could only add.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {TABLE_STOCK_SIGNAL_ALERT_DAILY} WHERE trade_date = %s AND kind = %s",
            (day, kind),
        )
        removed = max(int(cur.rowcount or 0), 0)
    if alerts:
        batch_upsert(
            conn,
            TABLE_STOCK_SIGNAL_ALERT_DAILY,
            UPSERT_COLS,
            alerts,
            conflict_keys=("trade_date", "kind", "symbol", "lens"),
            update_cols=("severity", "reason_json", "computed_at"),
            set_fetched_at=False,
            auto_commit=False,
        )
    conn.commit()
    return removed


def run(
    *,
    as_of: date | None = None,
    rejudge: int = REJUDGE_SCAN_DATES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Judge the newest scan date and re-judge composite_high on the ones before it.

    composite_high is a fact about one scan row, so it is re-judged on every date
    the scan may have recomputed. hit_rate_drop and weight_shift read trailing
    windows of forward hits that fill in later; re-judging an old date would
    rewrite it with hits nobody could see that day, so they are judged on the
    newest date only (replace semantics there too). ``dry_run`` computes and
    reports without writing.
    """
    conn = connect()
    try:
        day = _asof(conn, as_of)
        dates = _scan_dates(conn, day, max(1, rejudge))
        planned: list[tuple[date, str, list[tuple[Any, ...]]]] = [
            (d, "composite_high", _composite_high_alerts(conn, d)) for d in dates
        ]
        planned.append((day, "hit_rate_drop", _hit_rate_drop_alerts(conn, day)))
        planned.append((day, "weight_shift", _adaptive_weight_shift_alerts(conn, day)))
        removed = 0
        if not dry_run:
            for d, kind, alerts in planned:
                removed += _replace(conn, d, kind, alerts)
        by_kind: dict[str, int] = {}
        for _d, kind, alerts in planned:
            by_kind[kind] = by_kind.get(kind, 0) + len(alerts)
        out: dict[str, Any] = {
            "as_of": day.isoformat(),
            "judged_dates": [d.isoformat() for d in dates],
            "alerts_written": 0 if dry_run else sum(by_kind.values()),
            "alerts_removed": removed,
            "by_kind": by_kind,
            "composite_high": {
                d.isoformat(): [a[2] for a in alerts]
                for d, kind, alerts in planned
                if kind == "composite_high" and alerts
            },
            "kinds": sorted(k for k, n in by_kind.items() if n),
            "dry_run": dry_run,
        }
        if as_of is None:
            # The session the batch closed, for the output check: the judge must
            # read tonight's scan, not the previous night's (TD-97).
            from bifrost_research.db.calendar import latest_closed_session

            out["session"] = latest_closed_session(conn).isoformat()
        return out
    finally:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert_scan: closing the connection failed: %s", exc)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Build features.stock_signal_alert_daily")
    parser.add_argument("--as-of", type=str, default=None)
    parser.add_argument("--rejudge", type=int, default=REJUDGE_SCAN_DATES)
    parser.add_argument("--dry-run", action="store_true", help="compute and report, write nothing")
    args = parser.parse_args(list(argv) if argv is not None else None)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    result = run(as_of=as_of, rejudge=args.rejudge, dry_run=args.dry_run)
    logger.info("alert_scan result=%s", result)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
