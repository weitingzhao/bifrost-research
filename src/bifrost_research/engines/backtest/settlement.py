"""Backtest / Forecast Settlement engine (Wave 4.4).

Compares forecast sessions vs actual prices (injectable series or market.stock_daily).
Metrics: Path Hit, Close Miss, accuracy aggregates.

D10 BLOCKED — evaluation only; no order placement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from bifrost_research.db.upsert import batch_upsert

_SETTLEMENT_COLS = (
    "settlement_id",
    "session_id",
    "symbol",
    "trade_date",
    "expected_close",
    "actual_close",
    "close_miss",
    "close_miss_pct",
    "path_hit",
    "path_hit_count",
    "path_total",
    "hourly_json",
    "notes",
    "computed_at",
)

_BACKTEST_COLS = (
    "result_id",
    "symbol",
    "period_start",
    "period_end",
    "sessions_settled",
    "path_hit_rate",
    "avg_close_miss_pct",
    "median_close_miss_pct",
    "stats_json",
    "computed_at",
)


@dataclass(frozen=True)
class PriceBar:
    bar_date: date
    close: float
    high: float | None = None
    low: float | None = None


@dataclass(frozen=True)
class HourlyActual:
    hour_et: int
    path_call: str
    level_low: float
    level_high: float
    level_target: float
    actual_price: float | None
    hit: bool


@dataclass
class ForecastSettlement:
    settlement_id: str
    session_id: str
    symbol: str
    trade_date: date
    expected_close: float
    actual_close: float
    close_miss: float
    close_miss_pct: float
    path_hit: bool
    path_hit_count: int
    path_total: int
    hourly: list[HourlyActual] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trade_date"] = self.trade_date.isoformat()
        return d


@dataclass
class BacktestSummary:
    result_id: str
    symbol: str
    period_start: date
    period_end: date
    sessions_settled: int
    path_hit_rate: float
    avg_close_miss_pct: float
    median_close_miss_pct: float
    stats_json: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["period_start"] = self.period_start.isoformat()
        d["period_end"] = self.period_end.isoformat()
        return d


def _path_hit_for_hour(
    *,
    path_call: str,
    level_low: float,
    level_high: float,
    level_target: float,
    actual: float,
    prev_actual: float | None,
) -> bool:
    """Heuristic Path Hit: price stayed in band or moved in called direction."""
    lo, hi = (level_low, level_high) if level_low <= level_high else (level_high, level_low)
    in_band = lo <= actual <= hi
    near_target = abs(actual - level_target) / max(abs(level_target), 1e-9) <= 0.01
    call = path_call.lower()
    directional = False
    if prev_actual is not None:
        if "higher" in call or "bull" in call:
            directional = actual >= prev_actual
        elif "lower" in call or "bear" in call:
            directional = actual <= prev_actual
        elif "mean-revert" in call or "coil" in call:
            directional = in_band
    if "close" in call:
        return near_target or in_band
    return in_band or near_target or directional


def settle_forecast(
    *,
    session_id: str,
    symbol: str,
    trade_date: date,
    expected_close: float,
    hourly: Sequence[Mapping[str, Any]],
    actual_close: float,
    hourly_actuals: Mapping[int, float] | None = None,
    settlement_id: str | None = None,
) -> ForecastSettlement:
    """Settle one forecast session against actual close (+ optional hourly prints)."""
    miss = actual_close - expected_close
    miss_pct = miss / expected_close if expected_close else 0.0
    hits: list[HourlyActual] = []
    prev: float | None = None
    for h in hourly:
        hour = int(h.get("hour_et") or h.get("hour") or 0)
        actual_px = None
        if hourly_actuals and hour in hourly_actuals:
            actual_px = float(hourly_actuals[hour])
        elif hourly_actuals is None:
            # Without intraday prints, approximate with linear blend spot→close
            actual_px = actual_close
        hit = False
        if actual_px is not None:
            hit = _path_hit_for_hour(
                path_call=str(h.get("path_call") or ""),
                level_low=float(h.get("level_low") or 0),
                level_high=float(h.get("level_high") or 0),
                level_target=float(h.get("level_target") or expected_close),
                actual=actual_px,
                prev_actual=prev,
            )
            prev = actual_px
        hits.append(
            HourlyActual(
                hour_et=hour,
                path_call=str(h.get("path_call") or ""),
                level_low=float(h.get("level_low") or 0),
                level_high=float(h.get("level_high") or 0),
                level_target=float(h.get("level_target") or 0),
                actual_price=actual_px,
                hit=hit,
            )
        )
    hit_count = sum(1 for x in hits if x.hit)
    total = len(hits) or 1
    # Session path_hit: majority of hourly hits AND close within 1% of expected
    close_ok = abs(miss_pct) <= 0.01
    path_hit = (hit_count / total >= 0.5) and close_ok

    return ForecastSettlement(
        settlement_id=settlement_id or f"stl-{uuid4().hex[:10]}",
        session_id=session_id,
        symbol=symbol.strip().upper(),
        trade_date=trade_date,
        expected_close=float(expected_close),
        actual_close=float(actual_close),
        close_miss=round(miss, 6),
        close_miss_pct=round(miss_pct, 6),
        path_hit=path_hit,
        path_hit_count=hit_count,
        path_total=len(hits),
        hourly=hits,
        notes="D10 BLOCKED — settlement is advisory evaluation only",
    )


def aggregate_accuracy(
    settlements: Sequence[ForecastSettlement],
    *,
    symbol: str | None = None,
    result_id: str | None = None,
) -> BacktestSummary:
    """Aggregate Path Hit rate and Close Miss stats across settlements."""
    if not settlements:
        today = date.today()
        return BacktestSummary(
            result_id=result_id or f"bt-{uuid4().hex[:8]}",
            symbol=(symbol or "").upper(),
            period_start=today,
            period_end=today,
            sessions_settled=0,
            path_hit_rate=0.0,
            avg_close_miss_pct=0.0,
            median_close_miss_pct=0.0,
            stats_json={"empty": True},
        )
    sym = (symbol or settlements[0].symbol).upper()
    dates = sorted(s.trade_date for s in settlements)
    hits = sum(1 for s in settlements if s.path_hit)
    miss_pcts = sorted(abs(s.close_miss_pct) for s in settlements)
    n = len(miss_pcts)
    mid = n // 2
    median = miss_pcts[mid] if n % 2 == 1 else (miss_pcts[mid - 1] + miss_pcts[mid]) / 2
    avg = sum(miss_pcts) / n
    return BacktestSummary(
        result_id=result_id or f"bt-{uuid4().hex[:8]}",
        symbol=sym,
        period_start=dates[0],
        period_end=dates[-1],
        sessions_settled=len(settlements),
        path_hit_rate=round(hits / len(settlements), 4),
        avg_close_miss_pct=round(avg, 6),
        median_close_miss_pct=round(median, 6),
        stats_json={
            "path_hits": hits,
            "path_misses": len(settlements) - hits,
            "mean_abs_close_miss_pct": round(avg, 6),
            "advisory": "D10 BLOCKED",
        },
    )


def load_actual_close(conn: Any, symbol: str, trade_date: date) -> float | None:
    """Read close from market.stock_daily when DB available."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT close FROM raw_market.stock_daily
            WHERE symbol = %s AND bar_date = %s
            LIMIT 1
            """,
            (symbol.strip().upper(), trade_date),
            )
        row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return float(next(iter(row.values())))
    return float(row[0])


def upsert_settlement(conn: Any, settlement: ForecastSettlement) -> int:
    now = datetime.now(timezone.utc)
    return batch_upsert(
        conn,
        "features.stock_backtest_settlement",
        _SETTLEMENT_COLS,
        [
            (
                settlement.settlement_id,
                settlement.session_id,
                settlement.symbol,
                settlement.trade_date,
                settlement.expected_close,
                settlement.actual_close,
                settlement.close_miss,
                settlement.close_miss_pct,
                settlement.path_hit,
                settlement.path_hit_count,
                settlement.path_total,
                [asdict(h) for h in settlement.hourly],
                settlement.notes,
                now,
            )
        ],
        conflict_keys=("settlement_id",),
        set_fetched_at=False,
    )


def upsert_backtest_result(conn: Any, summary: BacktestSummary) -> int:
    now = datetime.now(timezone.utc)
    return batch_upsert(
        conn,
        "features.stock_backtest_results_period",
        _BACKTEST_COLS,
        [
            (
                summary.result_id,
                summary.symbol,
                summary.period_start,
                summary.period_end,
                summary.sessions_settled,
                summary.path_hit_rate,
                summary.avg_close_miss_pct,
                summary.median_close_miss_pct,
                summary.stats_json,
                now,
            )
        ],
        conflict_keys=("result_id",),
        set_fetched_at=False,
    )
