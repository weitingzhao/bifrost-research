"""MCP tools: research.discovery.* — read-only discovery / brief surfaces.

Calls existing engines and SELECT helpers — no write paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.engines.backtest.regime_stats import compute_regime_stats
from bifrost_research.engines.brief.synth import synthesize_daily_brief
from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, ok, with_conn


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value.strip()[:10])


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _row_dict(row: Any, cols: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return {c: row.get(c) for c in cols}
    return {c: row[i] for i, c in enumerate(cols)}


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.discovery.daily_brief_synth",
        description=(
            "Synthesize Daily Brief verdict bundle for a symbol/date. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def daily_brief_synth(symbol: str, trade_date: str | None = None) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            return ok(synthesize_daily_brief(conn, symbol, _parse_date(trade_date)))

        return with_conn(_run)

    @mcp.tool(
        name="research.discovery.sepa_daily",
        description=(
            "SEPA feature-store daily rows (features.stock_signal_sepa_daily). "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def sepa_daily(
        symbol: str | None = None,
        trade_date: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        cols = (
            "symbol",
            "trade_date",
            "asof_ts",
            "sepa_score",
            "grade",
            "stage",
            "computed_at",
        )

        def _run(conn: Any) -> dict[str, Any]:
            clauses: list[str] = []
            params: list[Any] = []
            if symbol:
                clauses.append("symbol = %s")
                params.append(symbol.strip().upper())
            td = _parse_date(trade_date)
            if td is not None:
                clauses.append("trade_date = %s")
                params.append(td)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"""
                SELECT {', '.join(cols)}
                FROM features.stock_signal_sepa_daily
                {where}
                ORDER BY trade_date DESC NULLS LAST, sepa_score DESC NULLS LAST
                LIMIT %s
            """
            params.append(max(1, min(int(limit), 200)))
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                raw = cur.fetchall() or []
            rows = [_row_dict(r, cols) for r in raw]
            return ok({"rows": rows, "count": len(rows)})

        return with_conn(_run)

    @mcp.tool(
        name="research.discovery.sepa_candidates",
        description=(
            "SEPA Feature Store candidate snapshot (latest trade_date). "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def sepa_candidates(limit: int = 50) -> dict[str, Any]:
        cols = ("symbol", "sepa_score", "grade", "stage", "trade_date")

        def _run(conn: Any) -> dict[str, Any]:
            lim = max(1, min(int(limit), 200))
            sql = """
                SELECT symbol, sepa_score, grade, stage, trade_date
                FROM features.stock_signal_sepa_daily
                WHERE trade_date = (
                    SELECT MAX(trade_date) FROM features.stock_signal_sepa_daily
                )
                ORDER BY sepa_score DESC NULLS LAST
                LIMIT %s
            """
            with conn.cursor() as cur:
                cur.execute(sql, (lim,))
                raw = cur.fetchall() or []
            rows = [_row_dict(r, cols) for r in raw]
            return ok({"rows": rows, "count": len(rows)})

        return with_conn(_run)

    @mcp.tool(
        name="research.discovery.event_radar",
        description=f"Recent Event Radar rows. {READ_ONLY_SUFFIX}",
    )
    def event_radar(
        limit: int = 50,
        include_dropped: bool = False,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        cols = (
            "event_id",
            "batch_id",
            "collected_at",
            "source",
            "subject",
            "event_summary",
            "affected_symbols",
            "direction",
            "certainty",
            "sentiment",
            "theme",
            "importance",
            "event_date",
            "dropped",
        )

        def _run(conn: Any) -> dict[str, Any]:
            clauses: list[str] = []
            params: list[Any] = []
            if batch_id:
                clauses.append("batch_id = %s")
                params.append(batch_id)
            if not include_dropped:
                clauses.append("(dropped IS NULL OR dropped = false)")
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"""
                SELECT {', '.join(cols)}
                FROM features.event_signal_radar_daily
                {where}
                ORDER BY collected_at DESC NULLS LAST, importance DESC NULLS LAST
                LIMIT %s
            """
            params.append(max(1, min(int(limit), 200)))
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                raw = cur.fetchall() or []
            rows = [_row_dict(r, cols) for r in raw]
            return ok({"rows": rows, "count": len(rows)})

        return with_conn(_run)

    @mcp.tool(
        name="research.discovery.momentum_radar",
        description=f"Momentum radar scores from Feature Store. {READ_ONLY_SUFFIX}",
    )
    def momentum_radar(
        symbol: str | None = None,
        trade_date: str | None = None,
        grade: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        cols = (
            "symbol",
            "trade_date",
            "score",
            "grade",
            "path",
            "z_sdt",
            "z_v",
            "computed_at",
        )

        def _run(conn: Any) -> dict[str, Any]:
            clauses: list[str] = []
            params: list[Any] = []
            sym = symbol.strip().upper() if symbol else None
            if sym:
                clauses.append("symbol = %s")
                params.append(sym)
            resolved = _parse_date(trade_date)
            if resolved is None and sym:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT MAX(trade_date) FROM features.stock_signal_momentum_daily "
                        "WHERE symbol = %s",
                        (sym,),
                    )
                    row = cur.fetchone()
                if row is not None:
                    resolved = _as_date(
                        row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
                    )
            if resolved is not None:
                clauses.append("trade_date = %s")
                params.append(resolved)
            if grade:
                clauses.append("grade = %s")
                params.append(grade.strip().upper())
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"""
                SELECT {', '.join(cols)}
                FROM features.stock_signal_momentum_daily
                {where}
                ORDER BY score DESC NULLS LAST, symbol ASC
                LIMIT %s
            """
            params.append(max(1, min(int(limit), 200)))
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                raw = cur.fetchall() or []
            rows = [_row_dict(r, cols) for r in raw]
            return ok(
                {
                    "rows": rows,
                    "count": len(rows),
                    "symbol": sym,
                    "trade_date": resolved.isoformat() if resolved else None,
                }
            )

        return with_conn(_run)

    @mcp.tool(
        name="research.discovery.forecast_sessions",
        description=f"List forecast / playbook sessions. {READ_ONLY_SUFFIX}",
    )
    def forecast_sessions(
        symbol: str | None = None,
        trade_date: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        cols = (
            "session_id",
            "symbol",
            "trade_date",
            "regime",
            "spot",
            "prob_rangy",
            "prob_bull",
            "prob_bear",
            "prob_squeeze",
            "expected_close",
            "narrative",
            "computed_at",
        )

        def _run(conn: Any) -> dict[str, Any]:
            clauses: list[str] = []
            params: list[Any] = []
            if symbol:
                clauses.append("symbol = %s")
                params.append(symbol.strip().upper())
            td = _parse_date(trade_date)
            if td is not None:
                clauses.append("trade_date = %s")
                params.append(td)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"""
                SELECT {', '.join(cols)}
                FROM features.stock_forecast_session
                {where}
                ORDER BY trade_date DESC, computed_at DESC
                LIMIT %s
            """
            params.append(max(1, min(int(limit), 200)))
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                raw = cur.fetchall() or []
            rows = [_row_dict(r, cols) for r in raw]
            return ok({"rows": rows, "count": len(rows)})

        return with_conn(_run)

    @mcp.tool(
        name="research.discovery.gex_intraday",
        description=f"Intraday GEX timeline for a symbol. {READ_ONLY_SUFFIX}",
    )
    def gex_intraday(symbol: str, trade_date: str | None = None) -> dict[str, Any]:
        cols = (
            "symbol",
            "trade_date",
            "asof_ts",
            "spot",
            "total_net_gex",
            "zero_gamma",
            "major_call_wall",
            "major_put_wall",
            "computed_at",
        )

        def _run(conn: Any) -> dict[str, Any]:
            sym = symbol.strip().upper()
            resolved = _parse_date(trade_date)
            if resolved is None:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT MAX(trade_date) FROM features.option_metric_gex_intraday "
                        "WHERE symbol = %s",
                        (sym,),
                    )
                    row = cur.fetchone()
                if row is not None:
                    resolved = _as_date(
                        row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
                    )
            if resolved is None:
                return ok({"rows": [], "count": 0, "symbol": sym, "trade_date": None})
            sql = f"""
                SELECT {', '.join(cols)}
                FROM features.option_metric_gex_intraday
                WHERE symbol = %s AND trade_date = %s
                ORDER BY asof_ts ASC
            """
            with conn.cursor() as cur:
                cur.execute(sql, (sym, resolved))
                raw = cur.fetchall() or []
            rows = [_row_dict(r, cols) for r in raw]
            return ok(
                {
                    "rows": rows,
                    "count": len(rows),
                    "symbol": sym,
                    "trade_date": resolved.isoformat(),
                }
            )

        return with_conn(_run)

    @mcp.tool(
        name="research.discovery.flow_sentiment",
        description=f"Order-flow sentiment rows for a symbol. {READ_ONLY_SUFFIX}",
    )
    def flow_sentiment(symbol: str, limit: int = 50) -> dict[str, Any]:
        cols = (
            "symbol",
            "trade_date",
            "call_notional",
            "put_notional",
            "sentiment_score",
            "call_volume",
            "put_volume",
            "pcr_volume",
            "data_source",
            "computed_at",
        )

        def _run(conn: Any) -> dict[str, Any]:
            sym = symbol.strip().upper()
            lim = max(1, min(int(limit), 200))
            sql = f"""
                SELECT {', '.join(cols)}
                FROM features.option_flow_sentiment_daily
                WHERE symbol = %s
                ORDER BY trade_date DESC NULLS LAST, sentiment_score DESC NULLS LAST
                LIMIT %s
            """
            with conn.cursor() as cur:
                cur.execute(sql, (sym, lim))
                raw = cur.fetchall() or []
            rows = [_row_dict(r, cols) for r in raw]
            return ok({"rows": rows, "count": len(rows), "symbol": sym})

        return with_conn(_run)

    @mcp.tool(
        name="research.discovery.regime_stats",
        description=(
            "Backtest regime hit-rate stats for a symbol. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def regime_stats(
        symbol: str,
        lookback_days: int = 60,
        current_regime: str | None = None,
    ) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            return ok(
                compute_regime_stats(
                    conn,
                    symbol,
                    lookback_days=lookback_days,
                    current_regime=current_regime,
                )
            )

        return with_conn(_run)
