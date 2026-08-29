"""Analyze Wave M — daily alert scan engine.

Writes features.stock_signal_alert_daily for:
- composite_high: composite_score >= 90 and rank top-5 on as-of date
- weight_shift: adaptive lens weight vs 30d mean > 1σ (informational)
- hit_rate_drop: weekly hot-side hit_rate_5d drops >= 8pp vs prior week
"""

from __future__ import annotations

import argparse
import logging
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.conn import connect
from bifrost_research.db.upsert import batch_upsert
from bifrost_research.schema.schemas import (
    TABLE_STOCK_SIGNAL_ALERT_DAILY,
    TABLE_STOCK_SIGNAL_LENS_HIT_DAILY,
    TABLE_STOCK_SIGNAL_SCAN_DAILY,
)

logger = logging.getLogger(__name__)
_NY = ZoneInfo("America/New_York")

LENSES = ("iv_rank", "vrp", "opex_pin")
UPSERT_COLS = (
    "trade_date",
    "kind",
    "symbol",
    "lens",
    "severity",
    "reason_json",
    "computed_at",
)


def _today_ny() -> date:
    return datetime.now(timezone.utc).astimezone(_NY).date()


def _asof(conn: Any, as_of: date | None) -> date:
    if as_of:
        return as_of
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(trade_date) FROM {TABLE_STOCK_SIGNAL_SCAN_DAILY}")
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    return _today_ny()


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


def run(*, as_of: date | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        day = _asof(conn, as_of)
        alerts = (
            _composite_high_alerts(conn, day)
            + _hit_rate_drop_alerts(conn, day)
            + _adaptive_weight_shift_alerts(conn, day)
        )
        if alerts:
            batch_upsert(
                conn,
                TABLE_STOCK_SIGNAL_ALERT_DAILY,
                UPSERT_COLS,
                alerts,
                conflict_keys=("trade_date", "kind", "symbol", "lens"),
                update_cols=("severity", "reason_json", "computed_at"),
                set_fetched_at=False,
            )
        return {
            "as_of": day.isoformat(),
            "alerts_written": len(alerts),
            "kinds": sorted({a[1] for a in alerts}),
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Build features.stock_signal_alert_daily")
    parser.add_argument("--as-of", type=str, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    result = run(as_of=as_of)
    logger.info("alert_scan result=%s", result)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
