"""Regime-conditioned settlement stats for Verdict historical context."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping


def compute_regime_stats(
    conn: Any,
    symbol: str,
    *,
    lookback_days: int = 60,
    current_regime: str | None = None,
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    cutoff = date.today() - timedelta(days=max(lookback_days, 1))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                f.regime,
                COUNT(*) AS n,
                AVG(CASE WHEN s.path_hit THEN 1.0 ELSE 0.0 END) AS path_hit_rate,
                AVG(ABS(s.close_miss_pct)) AS avg_close_miss_pct
            FROM features.stock_backtest_settlement s
            JOIN features.stock_forecast_session f ON f.session_id = s.session_id
            WHERE s.symbol = %s AND s.trade_date >= %s
            GROUP BY f.regime
            ORDER BY n DESC
            """,
            (sym, cutoff),
        )
        rows = cur.fetchall() or []

    by_regime: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            regime = row.get("regime")
            n = int(row.get("n") or 0)
            path_hit_rate = float(row.get("path_hit_rate") or 0)
            avg_close_miss_pct = float(row.get("avg_close_miss_pct") or 0)
        else:
            regime, n, path_hit_rate, avg_close_miss_pct = row[0], int(row[1] or 0), float(row[2] or 0), float(row[3] or 0)
        by_regime.append(
            {
                "regime": regime or "—",
                "n": n,
                "path_hit_rate": round(path_hit_rate, 4),
                "avg_close_miss_pct": round(avg_close_miss_pct, 6),
            }
        )

    current: dict[str, Any] | None = None
    if current_regime:
        match = next((r for r in by_regime if r["regime"] == current_regime), None)
        if match:
            current = {
                "regime": current_regime,
                "path_hit_rate": match["path_hit_rate"],
                "sample_n": match["n"],
                "lookback_days": lookback_days,
                "avg_close_miss_pct": match["avg_close_miss_pct"],
            }

    return {
        "symbol": sym,
        "lookback_days": lookback_days,
        "by_regime": by_regime,
        "current_regime": current,
    }
