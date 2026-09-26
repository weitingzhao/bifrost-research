"""Backtest / Forecast Settlement engine (Wave 4.4).

Compares forecast sessions vs actual prices (injectable series or market.stock_daily).
Metrics: Path Hit, Close Miss, accuracy aggregates.

D10 BLOCKED — evaluation only; no order placement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

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
    "stats_json",
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
    stats_json: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trade_date"] = self.trade_date.isoformat()
        d["direction_hit"] = self.stats_json.get("direction_hit")
        d["path_shape"] = self.stats_json.get("path_shape")
        d["close_zone"] = self.stats_json.get("close_zone")
        d["lean_miss"] = self.stats_json.get("lean_miss")
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
    spot: float | None = None,
    target_date: date | None = None,
    input_fault: str | None = None,
) -> ForecastSettlement:
    """Settle one forecast session against the close it forecast (+ hourly prints).

    A session is built after its trade date's close from that close's data, so what
    it forecasts is the session that follows: ``actual_close`` and
    ``hourly_actuals`` belong to that session (``target_date``), and ``spot`` is the
    close the forecast started from.

    An hour with no print is left unjudged. Until 2026-09-26 every such hour was
    scored against the day's close, so a settlement with no intraday prints still
    reported an hourly path hit rate it never measured; the path is now judged on
    the hours that printed, or on the close alone when none did
    (``stats_json.path_basis``).

    ``input_fault`` names an input the forecast was drawn from that did not
    describe the price (``terrain_input_fault``). The row is still written — the
    page shows what happened — but ``forecast_result_sql`` leaves it out of every
    rate and average, which would otherwise measure that input: PLTR's avg |miss|
    in range read 57% on the strength of July targets set at 20–65 against a
    120–130 stock.
    """
    miss = actual_close - expected_close
    miss_pct = miss / expected_close if expected_close else 0.0
    hits: list[HourlyActual] = []
    prev: float | None = None
    for h in hourly:
        hour = int(h.get("hour_et") or h.get("hour") or 0)
        actual_px = None
        if hourly_actuals and hour in hourly_actuals:
            actual_px = float(hourly_actuals[hour])
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
    judged = [x for x in hits if x.actual_price is not None]
    hit_count = sum(1 for x in judged if x.hit)
    # Session path_hit: majority of the judged hours AND close within 1% of
    # expected; with no hour judged, the close alone.
    close_ok = abs(miss_pct) <= 0.01
    if judged:
        path_hit = (hit_count / len(judged) >= 0.5) and close_ok
        path_basis = "hourly"
    else:
        path_hit = close_ok
        path_basis = "close"

    # Direction is the sign of the move the session called against the move that
    # came, both from its spot. «Close at or above the target» — what this was —
    # scores a call for a fall as right whenever the price rose.
    if spot is not None and spot > 0:
        called = expected_close - spot
        came = actual_close - spot
        if abs(called) / spot < 0.001:
            direction_hit = abs(came) / spot <= 0.005
        else:
            direction_hit = called * came > 0
    else:
        direction_hit = actual_close >= expected_close if expected_close else path_hit
    if abs(miss_pct) <= 0.005:
        path_shape = "flat"
    elif miss_pct > 0.01:
        path_shape = "up_miss"
    elif miss_pct < -0.01:
        path_shape = "down_miss"
    else:
        path_shape = "in_band"
    if abs(miss_pct) <= 0.01:
        close_zone = "on_target"
    elif abs(miss_pct) <= 0.03:
        close_zone = "near"
    else:
        close_zone = "far"
    lean_miss = not close_ok and (hit_count < len(judged) if judged else True)

    stats_json: dict[str, Any] = {
        "direction_hit": direction_hit,
        "path_shape": path_shape,
        "close_zone": close_zone,
        "lean_miss": lean_miss,
        "path_basis": path_basis,
        "hours_judged": len(judged),
        "forecast_hours": len(hits),
    }
    if spot is not None:
        stats_json["spot"] = float(spot)
    if target_date is not None:
        stats_json["target_date"] = target_date.isoformat()
    if input_fault:
        stats_json["input_fault"] = input_fault

    return ForecastSettlement(
        # One settlement per session: re-settling (the hourly basis arriving after
        # a close-only pass) replaces the row instead of adding a second.
        settlement_id=settlement_id or f"stl-{session_id}",
        session_id=session_id,
        symbol=symbol.strip().upper(),
        trade_date=trade_date,
        expected_close=float(expected_close),
        actual_close=float(actual_close),
        close_miss=round(miss, 6),
        close_miss_pct=round(miss_pct, 6),
        path_hit=path_hit,
        path_hit_count=hit_count,
        path_total=len(judged),
        hourly=hits,
        notes="D10 BLOCKED — settlement is advisory evaluation only",
        stats_json=stats_json,
    )


def forecast_result_sql(alias: str = "") -> str:
    """SQL predicate: this settlement scores the forecast, not a faulty input.

    Every rate or average over ``features.stock_backtest_settlement`` applies it;
    lists of rows do not, and show the fault instead.
    """
    col = f"{alias}.stats_json" if alias else "stats_json"
    return f"NOT COALESCE({col} ? 'input_fault', false)"


def input_fault_count_sql(alias: str = "") -> str:
    """``COUNT(*) FILTER (…)`` of the rows ``forecast_result_sql`` leaves out."""
    return f"COUNT(*) FILTER (WHERE NOT ({forecast_result_sql(alias)}))"


def aggregate_accuracy(
    settlements: Sequence[ForecastSettlement],
    *,
    symbol: str | None = None,
    result_id: str | None = None,
) -> BacktestSummary:
    """Aggregate Path Hit rate and Close Miss stats across settlements.

    Settlements stamped with an input fault are left out (``forecast_result_sql``)
    and counted in ``stats_json.input_faults``.
    """
    faults = sum(1 for s in settlements if (s.stats_json or {}).get("input_fault"))
    settlements = [s for s in settlements if not (s.stats_json or {}).get("input_fault")]
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
            stats_json={"empty": True, "input_faults": faults},
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
            "input_faults": faults,
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


_NY = ZoneInfo("America/New_York")


def load_hourly_closes(conn: Any, symbol: str, day: date) -> dict[int, float]:
    """The price at each whole hour ET of ``day``, keyed by that hour (10…16).

    The market-data plugin stores 1-hour bars labelled by their **start**
    (``raw_market.stock_minute``, period ``1 hour``; measured on PLTR 2026-09-24:
    the 15:00 ET bar closes at 192.62 against a daily close of 192.59), so the
    price at 10:00 ET is the close of the bar that started at 09:00 ET. The window
    is a ``bar_time`` range rather than a date cast so the primary key serves it.
    Empty when the name has no hourly bars — the plugin pulls them for its
    minute-bar scope only.
    """
    start = datetime.combine(day, time(0, 0), tzinfo=_NY)
    end = start + timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bar_time, close FROM raw_market.stock_minute
            WHERE symbol = %s AND period = '1 hour'
              AND bar_time >= %s AND bar_time < %s
            """,
            (symbol.strip().upper(), start, end),
        )
        rows = cur.fetchall() or []
    out: dict[int, float] = {}
    for row in rows:
        bar_time, close = (row["bar_time"], row["close"]) if isinstance(row, Mapping) else (row[0], row[1])
        if close is None:
            continue
        hour_end = bar_time.astimezone(_NY).hour + 1
        if 10 <= hour_end <= 16:
            out[hour_end] = float(close)
    return out


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
                settlement.stats_json,
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
