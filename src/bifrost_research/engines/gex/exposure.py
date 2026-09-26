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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.upsert import batch_upsert

_NY = ZoneInfo("America/New_York")

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
                "call_gex_vol": round(float(b["call_gex_vol"]), 4),
                "put_gex_vol": round(float(b["put_gex_vol"]), 4),
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


def _first_positive(row: Any) -> float | None:
    if row is None:
        return None
    v = row[0] if not isinstance(row, Mapping) else next(iter(row.values()))
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def parity_spot(conn: Any, underlying: str, trade_date: date) -> float | None:
    """The underlying implied by put–call parity on the day's newest chain.

    On the nearest expiry after ``trade_date``, ``F = K + C − P`` at each strike
    with both sides priced; the five strikes where C and P are closest bracket
    the money, and their median is the forward — a day or two of carry off spot,
    well inside a strike step. SPX 2026-09-25 read 7744.9–7745.5 across them, and
    SPY closed 771.35. Uses the newest snapshot of the day, so an intraday caller
    gets the session's own level.
    """
    start = datetime.combine(trade_date, datetime.min.time(), tzinfo=_NY)
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH s AS (
              SELECT oc.expiry, oc.strike, oc.option_right AS r, os.day_close AS px, os.snapshot_ts
              FROM raw_market.option_snapshot os
              JOIN raw_market.option_contract oc ON oc.option_ticker = os.option_ticker
              WHERE os.underlying = %s
                AND os.snapshot_ts >= %s AND os.snapshot_ts < %s
                AND os.day_close > 0
                AND oc.expiry > %s
            ),
            pick AS (SELECT MAX(snapshot_ts) AS ts, MIN(expiry) AS ex FROM s)
            SELECT c.strike + c.px - p.px AS fwd
            FROM s c
            JOIN s p ON p.strike = c.strike AND p.expiry = c.expiry
                    AND p.snapshot_ts = c.snapshot_ts AND p.r = 'P'
            JOIN pick ON c.snapshot_ts = pick.ts AND c.expiry = pick.ex
            WHERE c.r = 'C'
            ORDER BY ABS(c.px - p.px)
            LIMIT 5
            """,
            (underlying, start, start + timedelta(days=1), trade_date),
        )
        rows = cur.fetchall() if hasattr(cur, "fetchall") else []
    fwds = sorted(f for f in (_first_positive(r) for r in rows or []) if f is not None)
    if not fwds:
        return None
    return round(float(fwds[len(fwds) // 2]), 4)


def fetch_spot_reading(
    conn: Any,
    symbol: str,
    trade_date: date,
    *,
    prior_close_days: int = 0,
) -> tuple[float, str, date] | None:
    """Spot for ``trade_date`` with where it came from: ``(spot, source, as_of)``.

    ``source`` is ``close`` (that day's daily bar), ``snapshot`` (that day's stock
    snapshot), ``prior_close``, or for an index ``parity`` (``parity_spot``) and,
    failing that, ``oi_max_strike``.

    ``prior_close_days`` lets an intraday caller stand on the newest close up to
    that many days before ``trade_date``. Both exact-date tables are written after
    the close, so during the session they hold nothing for today: until
    2026-09-26 gex-intraday asked for today, found no spot for 668 of 669 names
    each half hour and reported success (terrain met the same wall on 09-02,
    b79773b). The daily path keeps the exact match — a stale close there would
    hide a missing bar.
    """
    sym = symbol.strip().upper()
    # Index options: OI stored as SPX; spot may live as SPX or Polygon I:SPX.
    candidates = [sym]
    if sym == "SPX":
        candidates.append("I:SPX")
    elif sym == "I:SPX":
        candidates.append("SPX")
        sym = "SPX"

    for table, col, source in (
        ("raw_market.stock_daily", "bar_date", "close"),
        ("raw_market.stock_snapshot", "session_date", "snapshot"),
    ):
        with conn.cursor() as cur:
            for cand in candidates:
                cur.execute(
                    f"SELECT close FROM {table} WHERE symbol = %s AND {col} = %s",
                    (cand, trade_date),
                )
                f = _first_positive(cur.fetchone())
                if f is not None:
                    return f, source, trade_date

    if prior_close_days > 0:
        with conn.cursor() as cur:
            for cand in candidates:
                cur.execute(
                    """
                    SELECT close, bar_date FROM raw_market.stock_daily
                    WHERE symbol = %s AND bar_date < %s AND bar_date >= %s
                    ORDER BY bar_date DESC
                    LIMIT 1
                    """,
                    (cand, trade_date, trade_date - timedelta(days=prior_close_days)),
                )
                row = cur.fetchone()
                f = _first_positive(row)
                if f is not None:
                    bar = row.get("bar_date") if isinstance(row, Mapping) else row[1]
                    return f, "prior_close", _as_date(bar) or trade_date

    # Indices spot bars may be entitlement-gated (Polygon I:SPX 403): no SPX price
    # sits in any raw_market table. The chain itself prices the index — see
    # ``parity_spot``. The max-OI strike stays as the last resort only: it read
    # 7000 for SPX on 2026-09-25 against 7745 by parity (SPY 771.35), so every
    # intraday SPX row put spot below zero γ and called dealers short gamma.
    if sym in ("SPX", "NDX", "RUT", "VIX") or sym.startswith("I:"):
        oi_sym = "SPX" if sym in ("SPX", "I:SPX") else sym.removeprefix("I:")
        f = parity_spot(conn, oi_sym, trade_date)
        if f is not None:
            return f, "parity", trade_date
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT strike
                FROM raw_market.option_open_interest
                WHERE underlying = %s AND trade_date = %s
                GROUP BY strike
                ORDER BY SUM(open_interest) DESC
                LIMIT 1
                """,
                (oi_sym, trade_date),
            )
            f = _first_positive(cur.fetchone())
        if f is not None:
            return f, "oi_max_strike", trade_date
    return None


def fetch_spot(conn: Any, symbol: str, trade_date: date) -> float | None:
    reading = fetch_spot_reading(conn, symbol, trade_date)
    return reading[0] if reading else None


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
        WHERE oi.underlying = %s
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


# A long weekend is four calendar days; a week covers it without reaching back
# to a close that no longer describes the name.
INTRADAY_PRIOR_CLOSE_DAYS = 7


def compute_gex_intraday(
    conn: Any,
    *,
    symbol: str,
    trade_date: date,
    asof_ts: datetime,
    expiry: date | None = None,
) -> dict[str, Any]:
    """Compute intraday GEX snapshot and write to features.option_metric_gex_intraday.

    Spot is the newest close up to a week back when the session has none yet
    (``spot_source`` says which); OI and gamma are the session's own, from the
    plugin's intraday chain.
    """
    reading = fetch_spot_reading(conn, symbol, trade_date, prior_close_days=INTRADAY_PRIOR_CLOSE_DAYS)
    if reading is None:
        return {"ok": False, "error": "No spot price", "symbol": symbol.strip().upper()}
    spot, spot_source, spot_date = reading

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
                "call_gex_vol": r.get("call_gex_vol"),
                "put_gex_vol": r.get("put_gex_vol"),
                "volume_net_gex": r.get("volume_net_gex"),
                "call_volume": r.get("call_volume"),
                "put_volume": r.get("put_volume"),
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
        "spot_source": spot_source,
        "spot_date": spot_date.isoformat(),
        "total_net_gex": levels["total_net_gex"],
        "zero_gamma": levels["zero_gamma"],
        "major_call_wall": levels["major_call_wall"],
        "major_put_wall": levels["major_put_wall"],
        "strikes": len(dist),
    }
