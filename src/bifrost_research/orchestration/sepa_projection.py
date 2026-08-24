"""Projection: dw_stock.mart_sepa_feature_daily → features.stock_signal_sepa_daily."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Sequence

from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_SEPA_DAILY

_PROJECTION_COLS = (
    "symbol",
    "trade_date",
    "fundamental_score",
    "trend_template_score",
    "momentum_score",
    "structure_score",
    "sepa_score",
    "grade",
    "stage",
    "path",
    "trend_template_pass",
    "fundamental_pass",
    "latest_close",
    "sma_50",
    "sma_150",
    "sma_200",
    "high_52w",
    "low_52w",
    "iv_percentile",
    "pcr_oi",
    "fund_pass_count",
    "tech_pass_count",
    "factors_json",
    "asof_ts",
    "computed_at",
)


def run_sepa_projection(
    conn: Any,
    *,
    trade_date: date | None = None,
    symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Copy dbt mart rows into Feature Store (PIT ``asof_ts`` stamped at projection time)."""
    resolved = trade_date
    with conn.cursor() as cur:
        if resolved is None:
            cur.execute("SELECT MAX(trade_date) FROM dw_stock.mart_sepa_feature_daily")
            row = cur.fetchone()
            if row is not None:
                val = row[0] if not isinstance(row, dict) else row.get("max")
                if val is not None:
                    resolved = val if isinstance(val, date) else date.fromisoformat(str(val)[:10])
        if resolved is None:
            return {
                "trade_date": None,
                "rows_written": 0,
                "skipped": True,
                "reason": "mart_sepa_feature_daily empty",
            }

        now = datetime.now(timezone.utc)
        params: list[Any] = [now, now, resolved]
        symbol_clause = ""
        if symbols:
            sym_list = [s.strip().upper() for s in symbols if s and str(s).strip()]
            if sym_list:
                symbol_clause = " AND m.symbol = ANY(%s)"
                params.append(sym_list)

        cur.execute(
            f"""
            INSERT INTO {TABLE_STOCK_SIGNAL_SEPA_DAILY} (
                symbol, trade_date, fundamental_score, trend_template_score,
                momentum_score, structure_score, sepa_score, grade, stage, path,
                trend_template_pass, fundamental_pass, latest_close, sma_50, sma_150,
                sma_200, high_52w, low_52w, iv_percentile, pcr_oi, fund_pass_count,
                tech_pass_count, factors_json, asof_ts, computed_at
            )
            SELECT
                m.symbol, m.trade_date, m.fundamental_score, m.trend_template_score,
                m.momentum_score, m.structure_score, m.sepa_score, m.grade, m.stage, m.path,
                m.trend_template_pass, m.fundamental_pass, m.latest_close, m.sma_50, m.sma_150,
                m.sma_200, m.high_52w, m.low_52w, m.iv_percentile, m.pcr_oi, m.fund_pass_count,
                m.tech_pass_count, m.factors_json, %s, %s
            FROM dw_stock.mart_sepa_feature_daily m
            WHERE m.trade_date = %s
            {symbol_clause}
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                fundamental_score = EXCLUDED.fundamental_score,
                trend_template_score = EXCLUDED.trend_template_score,
                momentum_score = EXCLUDED.momentum_score,
                structure_score = EXCLUDED.structure_score,
                sepa_score = EXCLUDED.sepa_score,
                grade = EXCLUDED.grade,
                stage = EXCLUDED.stage,
                path = EXCLUDED.path,
                trend_template_pass = EXCLUDED.trend_template_pass,
                fundamental_pass = EXCLUDED.fundamental_pass,
                latest_close = EXCLUDED.latest_close,
                sma_50 = EXCLUDED.sma_50,
                sma_150 = EXCLUDED.sma_150,
                sma_200 = EXCLUDED.sma_200,
                high_52w = EXCLUDED.high_52w,
                low_52w = EXCLUDED.low_52w,
                iv_percentile = EXCLUDED.iv_percentile,
                pcr_oi = EXCLUDED.pcr_oi,
                fund_pass_count = EXCLUDED.fund_pass_count,
                tech_pass_count = EXCLUDED.tech_pass_count,
                factors_json = EXCLUDED.factors_json,
                asof_ts = EXCLUDED.asof_ts,
                computed_at = EXCLUDED.computed_at
            """,
            tuple(params),
        )
        written = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
    conn.commit()
    return {
        "trade_date": resolved.isoformat(),
        "rows_written": written,
        "symbols": len(symbols) if symbols else None,
        "source": "dw_stock.mart_sepa_feature_daily",
    }
