#!/usr/bin/env python3
"""Compare raw_market.option_snapshot trade dates vs features.option_metric_atm_iv_daily."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from bifrost_research.db.conn import connect


def _distinct_trade_dates(conn, schema: str, table: str, date_col: str) -> set[date]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT {date_col}::date
            FROM {schema}.{table}
            WHERE {date_col} IS NOT NULL
            ORDER BY 1
            """
        )
        rows = cur.fetchall() or []
    out: set[date] = set()
    for row in rows:
        val = row[0] if not isinstance(row, dict) else row.get(date_col)
        if val is not None:
            out.add(val if isinstance(val, date) else date.fromisoformat(str(val)[:10]))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify volatility backfill coverage")
    parser.add_argument(
        "--min-trading-days",
        type=int,
        default=120,
        help="Minimum distinct trade_date count expected in features ATM IV",
    )
    args = parser.parse_args(argv)

    conn = connect()
    try:
        raw_dates = _distinct_trade_dates(
            conn,
            "raw_market",
            "option_snapshot",
            "snapshot_ts",
        )
        # NY session day from snapshot_ts for apples-to-apples with trade_date
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT DATE(timezone('America/New_York', snapshot_ts))::date AS d
                FROM raw_market.option_snapshot
                WHERE snapshot_ts IS NOT NULL
                ORDER BY 1
                """
            )
            raw_ny_days = {
                r[0]
                for r in (cur.fetchall() or [])
                if r[0] is not None
            }

        feat_dates = _distinct_trade_dates(
            conn,
            "features",
            "option_metric_atm_iv_daily",
            "trade_date",
        )

        missing = sorted(raw_ny_days - feat_dates)
        covered = sorted(feat_dates)
        ratio = (len(feat_dates) / len(raw_ny_days)) if raw_ny_days else 0.0

        report = {
            "raw_option_snapshot_ny_days": len(raw_ny_days),
            "features_atm_iv_days": len(feat_dates),
            "symbol_coverage_ratio": round(ratio, 4),
            "missing_dates": [d.isoformat() for d in missing],
            "missing_count": len(missing),
            "features_latest": covered[-1].isoformat() if covered else None,
            "features_earliest": covered[0].isoformat() if covered else None,
            "min_trading_days_required": args.min_trading_days,
            "ok": len(feat_dates) >= args.min_trading_days and len(missing) == 0,
        }
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
