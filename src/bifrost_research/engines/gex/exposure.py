"""GEX Engine — OI-based (and volume-based when available) gamma exposure.

Dealer-oriented sign convention (customer long calls / short puts → dealers short
call gamma / long put gamma):

  call_gex(K) = + gamma * OI * multiplier * spot^2 * 0.01
  put_gex(K)  = - gamma * OI * multiplier * spot^2 * 0.01

When gamma is missing, approximate BS ATM gamma using a flat IV assumption.

Levels:
  - Zero Gamma: strike where cumulative net GEX crosses zero (nearest)
  - Major Call Wall: strike with max call GEX
  - Major Put Wall: strike with min (most negative) put GEX

D10 BLOCKED — read-only analytics.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from bifrost_research.db.upsert import batch_upsert

_GEX_COLS = (
    "symbol",
    "trade_date",
    "expiry",
    "strike",
    "call_oi",
    "put_oi",
    "call_volume",
    "put_volume",
    "call_gex",
    "put_gex",
    "net_gex",
    "gex_source",
    "computed_at",
)

_LEVELS_COLS = (
    "symbol",
    "trade_date",
    "expiry",
    "spot",
    "total_net_gex",
    "zero_gamma",
    "major_call_wall",
    "major_put_wall",
    "call_wall_gex",
    "put_wall_gex",
    "computed_at",
)

MULTIPLIER = 100.0
DEFAULT_IV = 0.25
DEFAULT_T_YEARS = 30.0 / 365.0


@dataclass(frozen=True)
class ContractGreeks:
    strike: float
    option_right: str  # C / P
    open_interest: int
    volume: int = 0
    gamma: float | None = None


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def approx_bs_gamma(
    spot: float,
    strike: float,
    *,
    iv: float = DEFAULT_IV,
    t_years: float = DEFAULT_T_YEARS,
) -> float:
    """Black-Scholes gamma (per unit) with r=q=0."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * sqrt_t)
    return _norm_pdf(d1) / (spot * iv * sqrt_t)


def gex_notional(gamma: float, oi: int, spot: float, *, sign: float) -> float:
    """Signed GEX in dollars per 1% move convention."""
    if gamma <= 0 or oi <= 0 or spot <= 0:
        return 0.0
    return sign * gamma * float(oi) * MULTIPLIER * spot * spot * 0.01


def strike_gex_from_contracts(
    contracts: Sequence[ContractGreeks],
    spot: float,
    *,
    iv_fallback: float = DEFAULT_IV,
    t_years: float = DEFAULT_T_YEARS,
) -> list[dict[str, Any]]:
    """Aggregate per-strike OI/volume GEX. Pure function."""
    by_strike: dict[float, dict[str, Any]] = {}
    for c in contracts:
        sk = float(c.strike)
        if sk <= 0:
            continue
        bucket = by_strike.setdefault(
            sk,
            {
                "strike": sk,
                "call_oi": 0,
                "put_oi": 0,
                "call_volume": 0,
                "put_volume": 0,
                "call_gex": 0.0,
                "put_gex": 0.0,
                "call_gex_vol": 0.0,
                "put_gex_vol": 0.0,
                "has_live_gamma": False,
            },
        )
        right = (c.option_right or "").strip().upper()
        gamma = c.gamma
        if gamma is None or gamma <= 0:
            gamma = approx_bs_gamma(spot, sk, iv=iv_fallback, t_years=t_years)
        else:
            bucket["has_live_gamma"] = True

        if right in ("C", "CALL"):
            bucket["call_oi"] += int(c.open_interest or 0)
            bucket["call_volume"] += int(c.volume or 0)
            bucket["call_gex"] += gex_notional(gamma, int(c.open_interest or 0), spot, sign=1.0)
            if c.volume:
                bucket["call_gex_vol"] += gex_notional(gamma, int(c.volume), spot, sign=1.0)
        elif right in ("P", "PUT"):
            bucket["put_oi"] += int(c.open_interest or 0)
            bucket["put_volume"] += int(c.volume or 0)
            bucket["put_gex"] += gex_notional(gamma, int(c.open_interest or 0), spot, sign=-1.0)
            if c.volume:
                bucket["put_gex_vol"] += gex_notional(gamma, int(c.volume), spot, sign=-1.0)

    rows: list[dict[str, Any]] = []
    for sk in sorted(by_strike.keys()):
        b = by_strike[sk]
        net = float(b["call_gex"]) + float(b["put_gex"])
        vol_net = float(b["call_gex_vol"]) + float(b["put_gex_vol"])
        source = "oi_gamma" if b["has_live_gamma"] else "oi_approx_gamma"
        if vol_net != 0.0:
            source = source + "+volume"
        rows.append(
            {
                "strike": sk,
                "call_oi": int(b["call_oi"]),
                "put_oi": int(b["put_oi"]),
                "call_volume": int(b["call_volume"]),
                "put_volume": int(b["put_volume"]),
                "call_gex": round(float(b["call_gex"]), 4),
                "put_gex": round(float(b["put_gex"]), 4),
                "net_gex": round(net, 4),
                "volume_net_gex": round(vol_net, 4),
                "gex_source": source,
            }
        )
    return rows


def compute_gex_levels(distribution: Sequence[Mapping[str, Any]], spot: float) -> dict[str, Any]:
    """Derive Zero Gamma / Call Wall / Put Wall from strike distribution."""
    if not distribution:
        return {
            "spot": spot,
            "total_net_gex": 0.0,
            "zero_gamma": None,
            "major_call_wall": None,
            "major_put_wall": None,
            "call_wall_gex": None,
            "put_wall_gex": None,
        }

    total = sum(float(r.get("net_gex") or 0) for r in distribution)
    call_wall = max(distribution, key=lambda r: float(r.get("call_gex") or 0))
    put_wall = min(distribution, key=lambda r: float(r.get("put_gex") or 0))

    # Cumulative from low strike; find sign flip nearest spot
    sorted_rows = sorted(distribution, key=lambda r: float(r["strike"]))
    cum = 0.0
    zero_gamma: float | None = None
    prev_strike: float | None = None
    prev_cum = 0.0
    best_dist = float("inf")
    for r in sorted_rows:
        sk = float(r["strike"])
        cum += float(r.get("net_gex") or 0)
        if prev_strike is not None and prev_cum * cum <= 0 and (prev_cum != 0 or cum != 0):
            # Linear interpolate zero crossing
            if cum != prev_cum:
                t = -prev_cum / (cum - prev_cum)
                zg = prev_strike + t * (sk - prev_strike)
            else:
                zg = sk
            dist = abs(zg - spot)
            if dist < best_dist:
                best_dist = dist
                zero_gamma = round(zg, 4)
        prev_strike = sk
        prev_cum = cum

    if zero_gamma is None:
        # Fallback: strike with net_gex closest to zero near spot
        nearest = min(sorted_rows, key=lambda r: abs(float(r["strike"]) - spot))
        zero_gamma = float(nearest["strike"])

    return {
        "spot": float(spot),
        "total_net_gex": round(total, 4),
        "zero_gamma": zero_gamma,
        "major_call_wall": float(call_wall["strike"]),
        "major_put_wall": float(put_wall["strike"]),
        "call_wall_gex": round(float(call_wall.get("call_gex") or 0), 4),
        "put_wall_gex": round(float(put_wall.get("put_gex") or 0), 4),
    }


def compute_gex_distribution(
    contracts: Sequence[ContractGreeks],
    spot: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    dist = strike_gex_from_contracts(contracts, spot)
    levels = compute_gex_levels(dist, spot)
    return dist, levels


def _row_to_dict(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    if not s:
        return None
    return date.fromisoformat(s)


def fetch_spot(conn: Any, symbol: str, trade_date: date) -> float | None:
    sym = symbol.strip().upper()
    # Index options: OI stored as SPX; spot may live as SPX or Polygon I:SPX.
    candidates = [sym]
    if sym == "SPX":
        candidates.append("I:SPX")
    elif sym == "I:SPX":
        candidates.append("SPX")
        sym = "SPX"

    with conn.cursor() as cur:
        for cand in candidates:
            cur.execute(
                """
                SELECT close FROM raw_market.stock_daily
                WHERE UPPER(TRIM(symbol)) = %s AND bar_date = %s
                """,
                (cand, trade_date),
            )
            row = cur.fetchone()
            if row is not None:
                break
        else:
            row = None
    if row is None:
        with conn.cursor() as cur:
            for cand in candidates:
                cur.execute(
                    """
                    SELECT close FROM raw_market.stock_snapshot
                    WHERE UPPER(TRIM(symbol)) = %s AND session_date = %s
                    """,
                    (cand, trade_date),
                )
                row = cur.fetchone()
                if row is not None:
                    break
    if row is not None:
        v = row[0] if not isinstance(row, Mapping) else next(iter(row.values()))
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = None
        if f is not None and f > 0:
            return f

    # Indices spot bars may be entitlement-gated (Polygon I:SPX 403). Fall back to
    # the strike with maximum combined OI that day — a real market signal, not synthetic.
    if sym in ("SPX", "NDX", "RUT", "VIX") or sym.startswith("I:"):
        oi_sym = "SPX" if sym in ("SPX", "I:SPX") else sym.removeprefix("I:")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT strike
                FROM raw_market.option_open_interest
                WHERE UPPER(TRIM(underlying)) = %s AND trade_date = %s
                GROUP BY strike
                ORDER BY SUM(open_interest) DESC
                LIMIT 1
                """,
                (oi_sym, trade_date),
            )
            oi_row = cur.fetchone()
        if oi_row is not None:
            raw = oi_row[0] if not isinstance(oi_row, Mapping) else next(iter(oi_row.values()))
            try:
                strike = float(raw)
            except (TypeError, ValueError):
                return None
            return strike if strike > 0 else None
    return None


def fetch_gex_contracts(
    conn: Any,
    symbol: str,
    trade_date: date,
    *,
    expiry: date | None = None,
) -> list[tuple[date, ContractGreeks]]:
    """Join OI with latest same-day snapshot gamma/volume when available."""
    cols = (
        "expiry",
        "strike",
        "option_right",
        "open_interest",
        "gamma",
        "day_volume",
    )
    sql = """
        SELECT
          oi.expiry,
          oi.strike,
          oi.option_right,
          oi.open_interest,
          snap.gamma,
          snap.day_volume
        FROM raw_market.option_open_interest oi
        LEFT JOIN LATERAL (
          SELECT s.gamma, s.day_volume
          FROM raw_market.option_snapshot s
          WHERE s.option_ticker = oi.option_ticker
            AND DATE(timezone('America/New_York', s.snapshot_ts)) = %s
          ORDER BY s.snapshot_ts DESC
          LIMIT 1
        ) snap ON TRUE
        WHERE UPPER(TRIM(oi.underlying)) = %s
          AND oi.trade_date = %s
    """
    params: list[Any] = [trade_date, symbol.strip().upper(), trade_date]
    if expiry is not None:
        sql += " AND oi.expiry = %s"
        params.append(expiry)

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []

    out: list[tuple[date, ContractGreeks]] = []
    for r in raw or []:
        d = _row_to_dict(r, cols)
        exp = _as_date(d.get("expiry"))
        if exp is None:
            continue
        try:
            strike = float(d["strike"])
            oi = int(d.get("open_interest") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        gamma = None
        if d.get("gamma") is not None:
            try:
                gamma = float(d["gamma"])
            except (TypeError, ValueError):
                gamma = None
        vol = 0
        if d.get("day_volume") is not None:
            try:
                vol = int(d["day_volume"])
            except (TypeError, ValueError):
                vol = 0
        out.append(
            (
                exp,
                ContractGreeks(
                    strike=strike,
                    option_right=str(d.get("option_right") or ""),
                    open_interest=oi,
                    volume=vol,
                    gamma=gamma,
                ),
            )
        )
    return out


def compute_gex_for_symbol(
    conn: Any,
    *,
    symbol: str,
    trade_date: date,
    expiry: date | None = None,
) -> dict[str, Any]:
    spot = fetch_spot(conn, symbol, trade_date)
    if spot is None:
        return {
            "ok": False,
            "error": "No spot price",
            "symbol": symbol.strip().upper(),
            "trade_date": trade_date.isoformat(),
        }

    pairs = fetch_gex_contracts(conn, symbol, trade_date, expiry=expiry)
    if not pairs:
        return {
            "ok": False,
            "error": "No OI contracts",
            "symbol": symbol.strip().upper(),
            "trade_date": trade_date.isoformat(),
        }

    by_exp: dict[date, list[ContractGreeks]] = {}
    for exp, c in pairs:
        by_exp.setdefault(exp, []).append(c)

    now = datetime.now(timezone.utc)
    sym = symbol.strip().upper()
    dist_rows: list[tuple[Any, ...]] = []
    level_rows: list[tuple[Any, ...]] = []
    summaries: list[dict[str, Any]] = []

    for exp, contracts in sorted(by_exp.items()):
        dist, levels = compute_gex_distribution(contracts, spot)
        for r in dist:
            dist_rows.append(
                (
                    sym,
                    trade_date,
                    exp,
                    r["strike"],
                    r["call_oi"],
                    r["put_oi"],
                    r["call_volume"],
                    r["put_volume"],
                    r["call_gex"],
                    r["put_gex"],
                    r["net_gex"],
                    r["gex_source"],
                    now,
                )
            )
        level_rows.append(
            (
                sym,
                trade_date,
                exp,
                levels["spot"],
                levels["total_net_gex"],
                levels["zero_gamma"],
                levels["major_call_wall"],
                levels["major_put_wall"],
                levels["call_wall_gex"],
                levels["put_wall_gex"],
                now,
            )
        )
        summaries.append({"expiry": exp.isoformat(), **levels, "strikes": len(dist)})

    if dist_rows:
        batch_upsert(
            conn,
            "features.option_metric_gex_daily",
            _GEX_COLS,
            dist_rows,
            conflict_keys=("symbol", "trade_date", "expiry", "strike"),
            update_cols=(
                "call_oi",
                "put_oi",
                "call_volume",
                "put_volume",
                "call_gex",
                "put_gex",
                "net_gex",
                "gex_source",
                "computed_at",
            ),
            set_fetched_at=False,
        )
    if level_rows:
        batch_upsert(
            conn,
            "features.option_metric_gex_levels_daily",
            _LEVELS_COLS,
            level_rows,
            conflict_keys=("symbol", "trade_date", "expiry"),
            update_cols=(
                "spot",
                "total_net_gex",
                "zero_gamma",
                "major_call_wall",
                "major_put_wall",
                "call_wall_gex",
                "put_wall_gex",
                "computed_at",
            ),
            set_fetched_at=False,
        )

    return {
        "ok": True,
        "symbol": sym,
        "trade_date": trade_date.isoformat(),
        "spot": spot,
        "expiries": len(summaries),
        "distribution_rows": len(dist_rows),
        "levels": summaries,
    }


# ---------------------------------------------------------------------------
# Wave 6 — Intraday GEX Snapshot
# ---------------------------------------------------------------------------

_INTRADAY_COLS = (
    "symbol",
    "trade_date",
    "asof_ts",
    "spot",
    "total_net_gex",
    "zero_gamma",
    "major_call_wall",
    "major_put_wall",
    "levels_json",
    "computed_at",
)


def compute_gex_intraday(
    conn: Any,
    *,
    symbol: str,
    trade_date: date,
    asof_ts: datetime,
    expiry: date | None = None,
) -> dict[str, Any]:
    """Compute intraday GEX snapshot and write to features.option_metric_gex_intraday."""
    spot = fetch_spot(conn, symbol, trade_date)
    if spot is None:
        return {"ok": False, "error": "No spot price", "symbol": symbol.strip().upper()}

    pairs = fetch_gex_contracts(conn, symbol, trade_date, expiry=expiry)
    if not pairs:
        return {"ok": False, "error": "No OI contracts", "symbol": symbol.strip().upper()}

    all_contracts = [c for _, c in pairs]
    dist = strike_gex_from_contracts(all_contracts, spot)
    levels = compute_gex_levels(dist, spot)

    now = datetime.now(timezone.utc)
    sym = symbol.strip().upper()

    # Top-N strike GEX for levels_json
    top_strikes = sorted(dist, key=lambda r: abs(r.get("net_gex") or 0), reverse=True)[:50]
    levels_json = json.dumps(
        [
            {
                "strike": r["strike"],
                "call_gex": r["call_gex"],
                "put_gex": r["put_gex"],
                "net_gex": r["net_gex"],
            }
            for r in top_strikes
        ]
    )

    batch_upsert(
        conn,
        "features.option_metric_gex_intraday",
        _INTRADAY_COLS,
        [
            (
                sym,
                trade_date,
                asof_ts,
                levels["spot"],
                levels["total_net_gex"],
                levels["zero_gamma"],
                levels["major_call_wall"],
                levels["major_put_wall"],
                levels_json,
                now,
            )
        ],
        conflict_keys=("symbol", "trade_date", "asof_ts"),
        set_fetched_at=False,
    )

    return {
        "ok": True,
        "symbol": sym,
        "trade_date": trade_date.isoformat(),
        "asof_ts": asof_ts.isoformat(),
        "spot": spot,
        "total_net_gex": levels["total_net_gex"],
        "zero_gamma": levels["zero_gamma"],
        "major_call_wall": levels["major_call_wall"],
        "major_put_wall": levels["major_put_wall"],
        "strikes": len(dist),
    }
