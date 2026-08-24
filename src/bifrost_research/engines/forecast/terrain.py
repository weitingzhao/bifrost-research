"""Market Terrain / Analysis Model Dashboard (Wave 4.1).

Scores Pin / Trend Release / Vol Squeeze / Tail Risk, Expected Close,
Gamma Zone, and regime classification (crash-risk / range / trending).

Inputs are injectable dicts so unit tests never need live DB. When wired to
scheduler, signals come from features_option.gex_* + momentum + iv_surface / iv_percentile.

D10 BLOCKED — advisory only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal, Mapping, Sequence

from bifrost_research.db.upsert import batch_upsert

Regime = Literal["crash-risk", "range", "trending"]

_COLS = (
    "symbol",
    "trade_date",
    "pin_score",
    "trend_release",
    "vol_squeeze",
    "tail_risk",
    "expected_close",
    "gamma_zone_low",
    "gamma_zone_high",
    "regime",
    "spot",
    "inputs_json",
    "computed_at",
)


@dataclass(frozen=True)
class MarketTerrain:
    symbol: str
    trade_date: date
    pin_score: float
    trend_release: float
    vol_squeeze: float
    tail_risk: float
    expected_close: float
    gamma_zone_low: float
    gamma_zone_high: float
    regime: Regime
    spot: float
    inputs_json: dict[str, Any]

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.symbol,
            self.trade_date,
            self.pin_score,
            self.trend_release,
            self.vol_squeeze,
            self.tail_risk,
            self.expected_close,
            self.gamma_zone_low,
            self.gamma_zone_high,
            self.regime,
            self.spot,
            self.inputs_json,
            datetime.now(timezone.utc),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trade_date"] = self.trade_date.isoformat()
        return d


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _f(mapping: Mapping[str, Any] | None, key: str, default: float = 0.0) -> float:
    if not mapping:
        return default
    v = mapping.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pin_score_from_gex(
    *,
    spot: float,
    zero_gamma: float | None,
    call_wall: float | None,
    put_wall: float | None,
) -> float:
    """High pin when spot sits between put/call walls and near zero-gamma."""
    if spot <= 0:
        return 50.0
    score = 40.0
    if zero_gamma and zero_gamma > 0:
        dist = abs(spot - zero_gamma) / spot
        score += _clamp(40.0 * (1.0 - min(dist / 0.02, 1.0)), 0, 40)
    if call_wall and put_wall and call_wall > put_wall:
        if put_wall <= spot <= call_wall:
            width = (call_wall - put_wall) / spot
            score += _clamp(20.0 * (1.0 - min(width / 0.05, 1.0)), 0, 20)
        else:
            score -= 15.0
    return _clamp(score)


def trend_release_from_momentum(*, score: float | None, path: str | None) -> float:
    """High when momentum wants to release (EXT / high score); low when HALT."""
    base = _clamp(float(score) if score is not None else 50.0)
    path_u = (path or "").upper()
    if path_u == "HALT":
        return _clamp(base * 0.35)
    if path_u == "FAIL":
        return _clamp(base * 0.55)
    if path_u == "PB":
        return _clamp(base * 0.75)
    if path_u == "EXT":
        return _clamp(min(100.0, base * 1.05 + 5.0))
    return base


def vol_squeeze_from_iv(*, iv_percentile: float | None, iv_rank: float | None = None) -> float:
    """High squeeze when IV is compressed (low percentile / rank)."""
    pct = iv_percentile if iv_percentile is not None else iv_rank
    if pct is None:
        return 50.0
    # Invert: 10th pct → ~90 squeeze, 90th → ~10
    return _clamp(100.0 - float(pct))


def tail_risk_score(
    *,
    crash_factor: float | None,
    total_net_gex: float | None,
    momentum_score: float | None,
) -> float:
    """Higher = more left-tail risk."""
    crash = float(crash_factor) if crash_factor is not None else 50.0
    # crash factor in momentum is inverted (low crash → high contribution);
    # treat low crash_factor as higher tail risk.
    from_crash = _clamp(100.0 - crash)
    gex_penalty = 0.0
    if total_net_gex is not None and total_net_gex < 0:
        gex_penalty = _clamp(min(40.0, abs(total_net_gex) / 1e9 * 10.0), 0, 40)
    mom = float(momentum_score) if momentum_score is not None else 50.0
    weak_mom = _clamp(50.0 - mom) * 0.4
    return _clamp(0.5 * from_crash + gex_penalty + weak_mom)


def classify_regime(
    *,
    pin_score: float,
    trend_release: float,
    vol_squeeze: float,
    tail_risk: float,
) -> Regime:
    if tail_risk >= 65:
        return "crash-risk"
    if trend_release >= 70 and pin_score < 55 and vol_squeeze < 60:
        return "trending"
    if pin_score >= 55 or vol_squeeze >= 60:
        return "range"
    if trend_release >= 60:
        return "trending"
    return "range"


def expected_close_and_gamma_zone(
    *,
    spot: float,
    zero_gamma: float | None,
    call_wall: float | None,
    put_wall: float | None,
    regime: Regime,
    trend_release: float,
) -> tuple[float, float, float]:
    """Return (expected_close, zone_low, zone_high)."""
    zg = zero_gamma if zero_gamma and zero_gamma > 0 else spot
    low = put_wall if put_wall and put_wall > 0 else spot * 0.99
    high = call_wall if call_wall and call_wall > 0 else spot * 1.01
    if low > high:
        low, high = high, low

    if regime == "crash-risk":
        expected = min(spot, zg) * 0.995
    elif regime == "trending":
        drift = 0.004 * ((trend_release - 50.0) / 50.0)
        expected = spot * (1.0 + drift)
    else:
        # Range: pull toward mid of gamma zone / zero gamma
        mid = (low + high) / 2.0
        expected = 0.6 * zg + 0.4 * mid

    # Clamp expected into a soft band around zone
    expected = max(low * 0.98, min(high * 1.02, expected))
    return float(expected), float(low), float(high)


def compute_market_terrain(
    symbol: str,
    trade_date: date,
    *,
    spot: float,
    gex: Mapping[str, Any] | None = None,
    momentum: Mapping[str, Any] | None = None,
    iv: Mapping[str, Any] | None = None,
) -> MarketTerrain:
    """Compose terrain scores from optional upstream Wave 3 signal maps."""
    sym = symbol.strip().upper()
    zero_gamma = _f(gex, "zero_gamma", 0.0) or None
    call_wall = _f(gex, "major_call_wall", 0.0) or None
    put_wall = _f(gex, "major_put_wall", 0.0) or None
    if zero_gamma == 0.0:
        zero_gamma = None
    if call_wall == 0.0:
        call_wall = None
    if put_wall == 0.0:
        put_wall = None

    pin = pin_score_from_gex(
        spot=spot,
        zero_gamma=zero_gamma,
        call_wall=call_wall,
        put_wall=put_wall,
    )
    mom_score = _f(momentum, "score", 50.0)
    path = str(momentum.get("path") if momentum else "") or None
    trend = trend_release_from_momentum(score=mom_score, path=path)

    iv_pct: float | None = None
    iv_rank: float | None = None
    if iv:
        if iv.get("iv_percentile_1y") is not None:
            iv_pct = float(iv["iv_percentile_1y"])
        elif iv.get("iv_percentile") is not None:
            iv_pct = float(iv["iv_percentile"])
        if iv.get("iv_rank_1y") is not None:
            iv_rank = float(iv["iv_rank_1y"])
    squeeze = vol_squeeze_from_iv(iv_percentile=iv_pct, iv_rank=iv_rank)

    crash = _f(momentum, "crash", 50.0) if momentum else 50.0
    net_gex: float | None = None
    if gex and gex.get("total_net_gex") is not None:
        net_gex = float(gex["total_net_gex"])
    tail = tail_risk_score(
        crash_factor=crash,
        total_net_gex=net_gex,
        momentum_score=mom_score,
    )
    regime = classify_regime(
        pin_score=pin,
        trend_release=trend,
        vol_squeeze=squeeze,
        tail_risk=tail,
    )
    expected, zone_lo, zone_hi = expected_close_and_gamma_zone(
        spot=spot,
        zero_gamma=zero_gamma,
        call_wall=call_wall,
        put_wall=put_wall,
        regime=regime,
        trend_release=trend,
    )
    inputs = {
        "gex": dict(gex) if gex else {},
        "momentum": dict(momentum) if momentum else {},
        "iv": dict(iv) if iv else {},
        "advisory": "D10 BLOCKED — terrain is advisory only",
    }
    return MarketTerrain(
        symbol=sym,
        trade_date=trade_date,
        pin_score=round(pin, 2),
        trend_release=round(trend, 2),
        vol_squeeze=round(squeeze, 2),
        tail_risk=round(tail, 2),
        expected_close=round(expected, 4),
        gamma_zone_low=round(zone_lo, 4),
        gamma_zone_high=round(zone_hi, 4),
        regime=regime,
        spot=float(spot),
        inputs_json=inputs,
    )


_INTRADAY_COLS = (
    "symbol",
    "trade_date",
    "asof_ts",
    "pin_score",
    "trend_release",
    "vol_squeeze",
    "tail_risk",
    "expected_close",
    "gamma_zone_low",
    "gamma_zone_high",
    "regime",
    "spot",
    "prob_rangy",
    "prob_bull",
    "prob_bear",
    "prob_squeeze",
    "inputs_json",
    "computed_at",
)


@dataclass(frozen=True)
class TerrainIntraday:
    """Terrain snapshot with intraday timestamp and scenario probabilities."""

    symbol: str
    trade_date: date
    asof_ts: datetime
    pin_score: float
    trend_release: float
    vol_squeeze: float
    tail_risk: float
    expected_close: float
    gamma_zone_low: float
    gamma_zone_high: float
    regime: Regime
    spot: float
    prob_rangy: float
    prob_bull: float
    prob_bear: float
    prob_squeeze: float
    inputs_json: dict[str, Any]

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.symbol,
            self.trade_date,
            self.asof_ts,
            self.pin_score,
            self.trend_release,
            self.vol_squeeze,
            self.tail_risk,
            self.expected_close,
            self.gamma_zone_low,
            self.gamma_zone_high,
            self.regime,
            self.spot,
            self.prob_rangy,
            self.prob_bull,
            self.prob_bear,
            self.prob_squeeze,
            self.inputs_json,
            datetime.now(timezone.utc),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trade_date"] = self.trade_date.isoformat()
        d["asof_ts"] = self.asof_ts.isoformat()
        return d


def _scenario_probs(
    pin_score: float,
    trend_release: float,
    vol_squeeze: float,
    tail_risk: float,
) -> tuple[float, float, float, float]:
    """Heuristic scenario probability split (sums to ~1.0)."""
    raw_rangy = _clamp(pin_score * 0.6 + vol_squeeze * 0.4, 0, 100)
    raw_bull = _clamp(trend_release * 0.7 + (100 - tail_risk) * 0.3, 0, 100)
    raw_bear = _clamp(tail_risk * 0.6 + (100 - trend_release) * 0.4, 0, 100)
    raw_squeeze = _clamp(vol_squeeze * 0.7 + pin_score * 0.3, 0, 100)
    total = raw_rangy + raw_bull + raw_bear + raw_squeeze
    if total <= 0:
        return (0.25, 0.25, 0.25, 0.25)
    return (
        round(raw_rangy / total, 4),
        round(raw_bull / total, 4),
        round(raw_bear / total, 4),
        round(raw_squeeze / total, 4),
    )


def compute_terrain_intraday(
    symbol: str,
    trade_date: date,
    asof_ts: datetime,
    *,
    spot: float,
    gex: Mapping[str, Any] | None = None,
    momentum: Mapping[str, Any] | None = None,
    iv: Mapping[str, Any] | None = None,
) -> TerrainIntraday:
    """Same algorithm as ``compute_market_terrain`` with asof_ts + probabilities."""
    terrain = compute_market_terrain(
        symbol, trade_date, spot=spot, gex=gex, momentum=momentum, iv=iv
    )
    pr, pb, pbear, ps = _scenario_probs(
        terrain.pin_score, terrain.trend_release, terrain.vol_squeeze, terrain.tail_risk
    )
    return TerrainIntraday(
        symbol=terrain.symbol,
        trade_date=terrain.trade_date,
        asof_ts=asof_ts,
        pin_score=terrain.pin_score,
        trend_release=terrain.trend_release,
        vol_squeeze=terrain.vol_squeeze,
        tail_risk=terrain.tail_risk,
        expected_close=terrain.expected_close,
        gamma_zone_low=terrain.gamma_zone_low,
        gamma_zone_high=terrain.gamma_zone_high,
        regime=terrain.regime,
        spot=terrain.spot,
        prob_rangy=pr,
        prob_bull=pb,
        prob_bear=pbear,
        prob_squeeze=ps,
        inputs_json=terrain.inputs_json,
    )


def upsert_terrain_intraday(conn: Any, rows: Sequence[TerrainIntraday]) -> int:
    if not rows:
        return 0
    return batch_upsert(
        conn,
        "features_forecasts.terrain_intraday",
        _INTRADAY_COLS,
        [r.to_row() for r in rows],
        conflict_keys=("symbol", "trade_date", "asof_ts"),
        set_fetched_at=False,
    )


def upsert_market_terrain(conn: Any, rows: Sequence[MarketTerrain]) -> int:
    if not rows:
        return 0
    return batch_upsert(
        conn,
        "features_forecasts.market_terrain_daily",
        _COLS,
        [r.to_row() for r in rows],
        conflict_keys=("symbol", "trade_date"),
        set_fetched_at=False,
    )


def load_upstream_signals(
    conn: Any,
    symbol: str,
    trade_date: date,
) -> tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Best-effort load of spot + Wave 3 tables for terrain compute."""
    sym = symbol.strip().upper()
    spot = 0.0
    gex: dict[str, Any] = {}
    momentum: dict[str, Any] = {}
    iv: dict[str, Any] = {}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT zero_gamma, major_call_wall, major_put_wall, total_net_gex, spot
            FROM features_option.gex_levels_daily
            WHERE symbol = %s AND trade_date = %s
            ORDER BY expiry ASC
            LIMIT 1
            """,
            (sym, trade_date),
        )
        row = cur.fetchone()
        if row:
            if isinstance(row, Mapping):
                gex = dict(row)
                spot = float(gex.get("spot") or 0)
            else:
                gex = {
                    "zero_gamma": row[0],
                    "major_call_wall": row[1],
                    "major_put_wall": row[2],
                    "total_net_gex": row[3],
                    "spot": row[4],
                }
                spot = float(row[4] or 0)

        cur.execute(
            """
            SELECT score, path, crash
            FROM features_signals.momentum_score_daily
            WHERE symbol = %s AND trade_date = %s
            LIMIT 1
            """,
            (sym, trade_date),
        )
        row = cur.fetchone()
        if row:
            if isinstance(row, Mapping):
                momentum = dict(row)
            else:
                momentum = {"score": row[0], "path": row[1], "crash": row[2]}

        cur.execute(
            """
            SELECT iv_percentile_1y, iv_rank_1y
            FROM features_daily.iv_percentile_daily
            WHERE symbol = %s AND trade_date = %s
            LIMIT 1
            """,
            (sym, trade_date),
        )
        row = cur.fetchone()
        if row:
            if isinstance(row, Mapping):
                iv = dict(row)
            else:
                iv = {"iv_percentile_1y": row[0], "iv_rank_1y": row[1]}

        if spot <= 0:
            cur.execute(
                """
                SELECT close FROM raw_market.stock_daily
                WHERE symbol = %s AND bar_date = %s
                LIMIT 1
                """,
                (sym, trade_date),
            )
            row = cur.fetchone()
            if row:
                spot = float(row[0] if not isinstance(row, Mapping) else next(iter(row.values())))

    # Fallback for index underlyings (SPX etc.) that have no stock row:
    # derive spot from option_snapshot delta≈0.5 / max_pain (shared with
    # iv_surface engine to keep a single source of truth).
    if spot <= 0:
        try:
            from bifrost_research.engines.volatility.surface import fetch_spot_fallback

            fb = fetch_spot_fallback(conn, sym, trade_date)
            if fb and fb > 0:
                spot = float(fb)
                if not gex.get("spot"):
                    gex["spot"] = spot
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

    return spot, gex, momentum, iv
