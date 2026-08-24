"""Read-only helpers for Golden Source ``analytics.*`` SEPA marts.

Ported from Trade API ``bifrost_api.research.analytics_reader``; uses
``bifrost_research.db.conn`` instead of a private pool.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

from bifrost_research.db.conn import get_conn

# dbt SEPA marts are single-day snapshots (symbol unique). Readers must use the
# latest eval_date in the table — NOT CURRENT_DATE (timezone / Cron lag mismatch).
_FUND_EVAL_TABLE = "dw_stock.mart_sepa_fundamental_eval"
_TECH_EVAL_TABLE = "dw_stock.mart_sepa_technical_eval"

_ALLOWED_EVAL_TABLES = frozenset({_FUND_EVAL_TABLE, _TECH_EVAL_TABLE})


def latest_eval_date(cur: Any, table: str) -> Optional[date]:
    """Return MAX(eval_date) for a known mart table, or None if empty."""
    if table not in _ALLOWED_EVAL_TABLES:
        raise ValueError(f"unsupported eval table: {table}")
    cur.execute(f"SELECT MAX(eval_date) AS d FROM {table}")
    row = cur.fetchone()
    if not row:
        return None
    raw = row["d"] if isinstance(row, dict) else row[0]
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw)[:10])


# Fundamental condition column names in dw_stock.mart_sepa_fundamental_eval
FUND_CONDITION_COLUMNS = [
    "eps_q2q_ge_25pct",
    "rev_q2q_ge_25pct",
    "eps_acc_2q",
    "rev_acc_2q",
    "eps_3y_ge_15pct",
    "rev_3y_ge_15pct",
    "eps_acc_fy",
    "rev_acc_fy",
]

# Technical condition column names in dw_stock.mart_sepa_technical_eval
TECH_CONDITION_COLUMNS = [
    "avg_volume_50_gt_threshold",
    "close_ge_low52_x_1_3",
    "close_ge_high52_x_0_75",
    "sma50_gt_sma150",
    "sma50_gt_sma200",
    "sma150_gt_sma200",
    "sma200_rising_1m",
    "price_gt_sma50",
    "price_gt_sma150",
    "price_gt_sma200",
    "crs_ge_70",
]


def fetch_criteria_stats() -> Dict[str, Any]:
    """Read pre-aggregated criteria pass/fail from dw_stock.mart_sepa_criteria_stats."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT domain, stats FROM dw_stock.mart_sepa_criteria_stats")
            rows = cur.fetchall() or []

    result: Dict[str, Any] = {}
    for row in rows:
        domain = row.get("domain", "unknown")
        stats = row.get("stats")
        if isinstance(stats, dict):
            result[domain] = stats
        else:
            result[domain] = dict(row)
    return result


def fetch_fundamental_eval_single(symbol: str) -> Optional[Dict[str, Any]]:
    """Return latest fundamental eval row for a single symbol."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM dw_stock.mart_sepa_fundamental_eval
                WHERE symbol = %s
                ORDER BY eval_date DESC
                LIMIT 1
                """,
                (symbol.upper(),),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def fetch_technical_eval_single(symbol: str) -> Optional[Dict[str, Any]]:
    """Return latest technical eval row for a single symbol."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM dw_stock.mart_sepa_technical_eval
                WHERE symbol = %s
                ORDER BY eval_date DESC
                LIMIT 1
                """,
                (symbol.upper(),),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def fetch_fundamental_filter(condition_ids: List[str], *, limit: int = 500) -> List[Dict[str, Any]]:
    """Return symbols that pass ALL given fundamental conditions (latest snapshot)."""
    valid = [c for c in condition_ids if c in FUND_CONDITION_COLUMNS]
    if not valid:
        return []
    where_parts = [f"{col} = true" for col in valid]
    where_parts.append("insufficient_data = false")
    sql = (
        f"SELECT symbol, pass_count FROM {_FUND_EVAL_TABLE} "
        f"WHERE eval_date = %s AND {' AND '.join(where_parts)} "
        "ORDER BY pass_count DESC, symbol ASC "
        f"LIMIT {int(limit)}"
    )
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            as_of = latest_eval_date(cur, _FUND_EVAL_TABLE)
            if as_of is None:
                return []
            cur.execute(sql, (as_of,))
            return [dict(r) for r in (cur.fetchall() or [])]


def fetch_technical_filter(condition_ids: List[str], *, limit: int = 500) -> List[Dict[str, Any]]:
    """Return symbols that pass ALL given technical conditions (latest snapshot)."""
    valid = [c for c in condition_ids if c in TECH_CONDITION_COLUMNS]
    if not valid:
        return []
    where_parts = [f"{col} = true" for col in valid]
    sql = (
        f"SELECT symbol, pass_count FROM {_TECH_EVAL_TABLE} "
        f"WHERE eval_date = %s AND {' AND '.join(where_parts)} "
        "ORDER BY pass_count DESC, symbol ASC "
        f"LIMIT {int(limit)}"
    )
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            as_of = latest_eval_date(cur, _TECH_EVAL_TABLE)
            if as_of is None:
                return []
            cur.execute(sql, (as_of,))
            return [dict(r) for r in (cur.fetchall() or [])]


def fetch_fundamental_distribution_symbols(conditions_passed: int) -> List[Dict[str, Any]]:
    """Return symbols with exactly N fundamental conditions passed (latest snapshot)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            as_of = latest_eval_date(cur, _FUND_EVAL_TABLE)
            if as_of is None:
                return []
            cur.execute(
                f"""
                SELECT symbol, pass_count,
                       eps_q2q_ge_25pct, rev_q2q_ge_25pct,
                       eps_acc_2q, rev_acc_2q,
                       eps_3y_ge_15pct, rev_3y_ge_15pct,
                       eps_acc_fy, rev_acc_fy
                FROM {_FUND_EVAL_TABLE}
                WHERE eval_date = %s
                  AND insufficient_data = false
                  AND pass_count = %s
                ORDER BY symbol
                """,
                (as_of, conditions_passed),
            )
            rows = cur.fetchall() or []

    result = []
    for r in rows:
        passed_conditions = [col for col in FUND_CONDITION_COLUMNS if r.get(col) is True]
        result.append({
            "symbol": r["symbol"],
            "pass_count": int(r.get("pass_count") or 0),
            "passed_conditions": passed_conditions,
        })
    return result


def fetch_technical_distribution_symbols(conditions_passed: int) -> List[Dict[str, Any]]:
    """Return symbols with exactly N technical conditions passed (latest snapshot)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            as_of = latest_eval_date(cur, _TECH_EVAL_TABLE)
            if as_of is None:
                return []
            cur.execute(
                f"""
                SELECT symbol, pass_count,
                       avg_volume_50_gt_threshold, close_ge_low52_x_1_3,
                       close_ge_high52_x_0_75, sma50_gt_sma150,
                       sma50_gt_sma200, sma150_gt_sma200,
                       sma200_rising_1m, price_gt_sma50,
                       price_gt_sma150, price_gt_sma200,
                       crs_ge_70
                FROM {_TECH_EVAL_TABLE}
                WHERE eval_date = %s
                  AND pass_count = %s
                ORDER BY symbol
                """,
                (as_of, conditions_passed),
            )
            rows = cur.fetchall() or []

    result = []
    for r in rows:
        passed_conditions = [col for col in TECH_CONDITION_COLUMNS if r.get(col) is True]
        result.append({
            "symbol": r["symbol"],
            "pass_count": int(r.get("pass_count") or 0),
            "passed_conditions": passed_conditions,
        })
    return result


def peek_latest_eval_date(table: str) -> Optional[str]:
    """ISO date of MAX(eval_date) for a known mart, or None."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            d = latest_eval_date(cur, table)
    return d.isoformat() if d else None


def fetch_screener_wide(
    symbols: Optional[List[str]] = None,
    *,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Read from dw_stock.mart_sepa_screener_wide."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if symbols:
                syms = [s.upper() for s in symbols[:500]]
                cur.execute(
                    """
                    SELECT *
                    FROM dw_stock.mart_sepa_screener_wide
                    WHERE symbol = ANY(%s)
                    ORDER BY symbol
                    """,
                    (syms,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT *
                    FROM dw_stock.mart_sepa_screener_wide
                    ORDER BY overall_rank ASC NULLS LAST
                    LIMIT {int(limit)}
                    """
                )
            return [dict(r) for r in (cur.fetchall() or [])]


def fetch_screening_ranked(*, limit: int = 500) -> List[Dict[str, Any]]:
    """Read composite scores and rankings from dw_stock.mart_sepa_screening_ranked."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT symbol, composite_score, overall_rank, decile, percentile
                FROM dw_stock.mart_sepa_screening_ranked
                ORDER BY overall_rank ASC NULLS LAST
                LIMIT {int(limit)}
                """
            )
            return [dict(r) for r in (cur.fetchall() or [])]
