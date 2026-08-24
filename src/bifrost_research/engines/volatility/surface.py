"""IV Surface / smile / vol-cone analysis (Wave 3.3).

Extends engines/volatility. Writes ``features.option_surface_iv_daily``.

Fits:
  - Polynomial smile on moneyness (degree 2 default; pure Python)
  - Lightweight SVI grid search (no scipy) when enough points

Vol cone: historical ATM IV percentile bands from features.option_metric_atm_iv_daily.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Mapping, Sequence

from bifrost_research.db.upsert import batch_upsert

_COLS = (
    "symbol",
    "trade_date",
    "expiry",
    "spot",
    "fit_model",
    "smile_params",
    "surface_points",
    "vol_cone",
    "rmse",
    "n_points",
    "computed_at",
)


@dataclass(frozen=True)
class IvPoint:
    strike: float
    iv: float
    option_right: str = ""


def _valid_iv(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not (0.0 < f < 10.0):
        return None
    return f


def moneyness(strike: float, spot: float) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    return math.log(strike / spot)


def _matmul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    n, m, p = len(a), len(b[0]), len(b)
    out = [[0.0] * m for _ in range(n)]
    for i in range(n):
        for k in range(p):
            aik = a[i][k]
            for j in range(m):
                out[i][j] += aik * b[k][j]
    return out


def _transpose(a: list[list[float]]) -> list[list[float]]:
    return [list(row) for row in zip(*a)]


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. Returns None if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        div = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= div
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col]
            for j in range(col, n + 1):
                m[r][j] -= factor * m[col][j]
    return [m[i][n] for i in range(n)]


def fit_polynomial_smile(
    points: Sequence[IvPoint],
    spot: float,
    *,
    degree: int = 2,
) -> dict[str, Any] | None:
    """Fit IV = sum_{k=0..degree} c_k * k^k  where k = log(K/S)."""
    if degree < 1 or degree > 4:
        raise ValueError("degree must be 1..4")
    xs: list[float] = []
    ys: list[float] = []
    for p in points:
        iv = _valid_iv(p.iv)
        if iv is None or p.strike <= 0:
            continue
        xs.append(moneyness(p.strike, spot))
        ys.append(iv)
    if len(xs) < degree + 1:
        return None

    # Design matrix
    x_mat = [[x**k for k in range(degree + 1)] for x in xs]
    xt = _transpose(x_mat)
    xtx = _matmul(xt, x_mat)
    xty = [sum(xt[i][j] * ys[j] for j in range(len(ys))) for i in range(degree + 1)]
    coeffs = _solve_linear(xtx, xty)
    if coeffs is None:
        return None

    preds = [sum(coeffs[k] * (x**k) for k in range(degree + 1)) for x in xs]
    rmse = math.sqrt(sum((preds[i] - ys[i]) ** 2 for i in range(len(ys))) / len(ys))
    return {
        "model": "polynomial",
        "degree": degree,
        "coeffs": [round(c, 8) for c in coeffs],
        "rmse": round(rmse, 8),
        "n_points": len(xs),
    }


def eval_polynomial(coeffs: Sequence[float], k: float) -> float:
    return sum(float(coeffs[i]) * (k**i) for i in range(len(coeffs)))


def svi_total_var(k: float, a: float, b: float, rho: float, m: float, sigma: float) -> float:
    """Raw SVI: w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))."""
    return a + b * (rho * (k - m) + math.sqrt((k - m) ** 2 + sigma**2))


def fit_svi_smile(
    points: Sequence[IvPoint],
    spot: float,
    *,
    t_years: float = 30.0 / 365.0,
) -> dict[str, Any] | None:
    """Coarse grid-search SVI on total variance (no scipy)."""
    if t_years <= 0:
        return None
    ks: list[float] = []
    ws: list[float] = []
    for p in points:
        iv = _valid_iv(p.iv)
        if iv is None or p.strike <= 0:
            continue
        ks.append(moneyness(p.strike, spot))
        ws.append(iv * iv * t_years)
    if len(ks) < 5:
        return None

    # Anchor a near median w
    w_med = median(ws)
    best: tuple[float, tuple[float, float, float, float, float]] | None = None
    for a in (w_med * 0.5, w_med * 0.8, w_med, w_med * 1.2):
        for b in (0.05, 0.1, 0.2, 0.4):
            for rho in (-0.6, -0.3, 0.0, 0.3):
                for m in (-0.1, 0.0, 0.1):
                    for sigma in (0.05, 0.1, 0.2):
                        if b * (1 + abs(rho)) >= 4 * a and a <= 0:
                            continue
                        sse = 0.0
                        for k, w in zip(ks, ws):
                            pred = svi_total_var(k, a, b, rho, m, sigma)
                            if pred <= 0:
                                sse = float("inf")
                                break
                            sse += (pred - w) ** 2
                        if best is None or sse < best[0]:
                            best = (sse, (a, b, rho, m, sigma))
    if best is None or not math.isfinite(best[0]):
        return None
    a, b, rho, m, sigma = best[1]
    rmse_w = math.sqrt(best[0] / len(ks))
    # Convert RMSE on w to rough IV RMSE
    rmse_iv = math.sqrt(max(rmse_w / max(t_years, 1e-8), 0.0))
    return {
        "model": "svi",
        "params": {
            "a": round(a, 8),
            "b": round(b, 8),
            "rho": round(rho, 8),
            "m": round(m, 8),
            "sigma": round(sigma, 8),
            "t_years": round(t_years, 8),
        },
        "rmse": round(rmse_iv, 8),
        "n_points": len(ks),
    }


def build_surface_grid(
    points_by_expiry: Mapping[date, Sequence[IvPoint]],
    spot: float,
    *,
    fit: str = "polynomial",
) -> list[dict[str, Any]]:
    """Strike × expiry IV grid using fitted smile (fallback to nearest raw IV)."""
    surface: list[dict[str, Any]] = []
    for exp, pts in sorted(points_by_expiry.items()):
        poly = fit_polynomial_smile(pts, spot) if fit != "svi" else None
        svi = fit_svi_smile(pts, spot) if fit == "svi" else None
        strikes = sorted({p.strike for p in pts if p.strike > 0})
        # densify a bit around spot
        if spot > 0:
            for pct in (-0.1, -0.05, 0.0, 0.05, 0.1):
                strikes.append(round(spot * (1 + pct), 4))
        strikes = sorted(set(round(s, 4) for s in strikes))
        for sk in strikes:
            k = moneyness(sk, spot)
            iv: float | None = None
            model = "raw"
            if poly and poly.get("coeffs"):
                iv = eval_polynomial(poly["coeffs"], k)
                model = "polynomial"
            elif svi and svi.get("params"):
                p = svi["params"]
                w = svi_total_var(k, p["a"], p["b"], p["rho"], p["m"], p["sigma"])
                t = float(p["t_years"])
                iv = math.sqrt(max(w / t, 0.0)) if t > 0 else None
                model = "svi"
            if iv is None or iv <= 0:
                # nearest raw
                nearest = min(pts, key=lambda p: abs(p.strike - sk))
                iv = _valid_iv(nearest.iv)
                model = "nearest"
            if iv is None:
                continue
            surface.append(
                {
                    "expiry": exp.isoformat(),
                    "strike": sk,
                    "iv": round(float(iv), 6),
                    "moneyness": round(k, 6),
                    "model": model,
                }
            )
    return surface


def vol_cone_from_history(
    history_ivs: Sequence[float],
    *,
    percentiles: Sequence[float] = (10, 25, 50, 75, 90),
) -> dict[str, Any]:
    """Historical IV percentile bands (vol cone slice for one tenor proxy)."""
    vals = sorted(float(v) for v in history_ivs if v is not None and float(v) > 0)
    if not vals:
        return {"n": 0, "bands": {}}
    bands: dict[str, float] = {}
    n = len(vals)
    for p in percentiles:
        if n == 1:
            bands[f"p{int(p)}"] = vals[0]
            continue
        idx = (p / 100.0) * (n - 1)
        lo = int(math.floor(idx))
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        bands[f"p{int(p)}"] = round(vals[lo] * (1 - frac) + vals[hi] * frac, 6)
    return {
        "n": n,
        "min": round(vals[0], 6),
        "max": round(vals[-1], 6),
        "bands": bands,
    }


def fit_iv_surface(
    points_by_expiry: Mapping[date, Sequence[IvPoint]],
    spot: float,
    *,
    history_ivs: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Fit per-expiry smiles + surface grid + optional vol cone."""
    smiles: list[dict[str, Any]] = []
    for exp, pts in sorted(points_by_expiry.items()):
        poly = fit_polynomial_smile(pts, spot)
        svi = fit_svi_smile(pts, spot)
        chosen = poly
        fit_model = "polynomial"
        if svi and (poly is None or (svi.get("rmse") or 99) < (poly.get("rmse") or 99)):
            # Prefer poly for stability; use SVI only if clearly better RMSE and enough pts
            if poly is None or (svi.get("n_points", 0) >= 7 and (svi["rmse"] + 0.01) < poly["rmse"]):
                chosen = svi
                fit_model = "svi"
        if chosen is None:
            continue
        smiles.append(
            {
                "expiry": exp.isoformat(),
                "fit_model": fit_model,
                "params": chosen.get("coeffs") or chosen.get("params"),
                "rmse": chosen.get("rmse"),
                "n_points": chosen.get("n_points"),
                "polynomial": poly,
                "svi": svi,
            }
        )

    surface = build_surface_grid(points_by_expiry, spot, fit="polynomial")
    cone = vol_cone_from_history(history_ivs or [])
    return {
        "spot": spot,
        "smiles": smiles,
        "surface_points": surface,
        "vol_cone": cone,
        "n_expiries": len(smiles),
    }


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


def fetch_iv_points_for_date(
    conn: Any,
    symbol: str,
    trade_date: date,
) -> tuple[float | None, dict[date, list[IvPoint]]]:
    cols = ("expiry", "strike", "option_right", "iv", "underlying_price")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (v.option_ticker)
              oc.expiry, oc.strike, oc.option_right, v.iv, v.underlying_price
            FROM raw_market.v_option_snapshot_with_stock v
            INNER JOIN raw_market.option_contract oc ON oc.option_ticker = v.option_ticker
            WHERE UPPER(TRIM(v.underlying)) = %s
              AND DATE(timezone('America/New_York', v.snapshot_ts)) = %s
              AND v.iv IS NOT NULL
            ORDER BY v.option_ticker, v.snapshot_ts DESC
            """,
            (symbol.strip().upper(), trade_date),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []

    by_exp: dict[date, list[IvPoint]] = {}
    spots: list[float] = []
    for r in raw or []:
        d = _row_to_dict(r, cols)
        exp = _as_date(d.get("expiry"))
        iv = _valid_iv(d.get("iv"))
        if exp is None or iv is None:
            continue
        try:
            strike = float(d["strike"])
        except (TypeError, ValueError, KeyError):
            continue
        if d.get("underlying_price") is not None:
            try:
                up = float(d["underlying_price"])
                if up > 0:
                    spots.append(up)
            except (TypeError, ValueError):
                pass
        by_exp.setdefault(exp, []).append(
            IvPoint(strike=strike, iv=iv, option_right=str(d.get("option_right") or ""))
        )
    spot = float(median(spots)) if spots else None
    # Fallback: view join yields no spot (e.g. index options like SPX have no
    # stock row) — derive an ATM proxy from option_snapshot delta ≈ 0.5.
    if spot is None and by_exp:
        spot = fetch_spot_fallback(conn, symbol, trade_date)
    return spot, by_exp


def fetch_spot_fallback(
    conn: Any,
    symbol: str,
    trade_date: date,
) -> float | None:
    """Derive an ATM spot proxy when stock join is unavailable.

    Prefers option_snapshot delta ≈ 0.5 call strike (works for indices like
    SPX without a stock row). Falls back to max_pain_daily.max_pain_strike if
    delta samples are unavailable.
    """
    sym = symbol.strip().upper()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT oc.strike, os.delta
                FROM raw_market.option_snapshot os
                JOIN raw_market.option_contract oc ON oc.option_ticker = os.option_ticker
                WHERE UPPER(TRIM(os.underlying)) = %s
                  AND DATE(timezone('America/New_York', os.snapshot_ts)) = %s
                  AND os.delta IS NOT NULL
                  AND os.delta BETWEEN 0.4 AND 0.6
                  AND oc.option_right = 'C'
                ORDER BY ABS(os.delta - 0.5)
                LIMIT 5
                """,
                (sym, trade_date),
            )
            rows = cur.fetchall() if hasattr(cur, "fetchall") else []
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        rows = []
    strikes: list[float] = []
    for r in rows or []:
        if isinstance(r, Mapping):
            k = r.get("strike")
        else:
            k = r[0] if r else None
        if k is None:
            continue
        try:
            kf = float(k)
        except (TypeError, ValueError):
            continue
        if kf > 0:
            strikes.append(kf)
    if strikes:
        return float(median(strikes))
    # Last-resort fallback: max_pain strike (less precise but coarse anchor)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT max_pain_strike
                FROM features.option_metric_max_pain_daily
                WHERE UPPER(TRIM(symbol)) = %s AND trade_date = %s
                ORDER BY expiry ASC
                LIMIT 1
                """,
                (sym, trade_date),
            )
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    if not row:
        return None
    if isinstance(row, Mapping):
        v = row.get("max_pain_strike")
    else:
        v = row[0]
    if v is None:
        return None
    try:
        vf = float(v)
    except (TypeError, ValueError):
        return None
    return vf if vf > 0 else None


def fetch_atm_iv_history(
    conn: Any,
    symbol: str,
    trade_date: date,
    *,
    lookback_days: int = 252,
) -> list[float]:
    start = trade_date - timedelta(days=lookback_days + 30)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, atm_iv
            FROM features.option_metric_atm_iv_daily
            WHERE UPPER(TRIM(symbol)) = %s
              AND trade_date >= %s AND trade_date <= %s
            """,
            (symbol.strip().upper(), start, trade_date),
        )
        raw = cur.fetchall() or []
    # Median per day then flatten
    by_day: dict[date, list[float]] = {}
    for r in raw:
        if isinstance(r, Mapping):
            td = _as_date(r.get("trade_date"))
            iv = r.get("atm_iv")
        else:
            td = _as_date(r[0])
            iv = r[1]
        if td is None:
            continue
        viv = _valid_iv(iv)
        if viv is None:
            continue
        by_day.setdefault(td, []).append(viv)
    return [float(median(vs)) for _, vs in sorted(by_day.items())]


def compute_iv_surface_for_symbol(
    conn: Any,
    *,
    symbol: str,
    trade_date: date,
) -> dict[str, Any]:
    spot, by_exp = fetch_iv_points_for_date(conn, symbol, trade_date)
    if spot is None or not by_exp:
        return {
            "ok": False,
            "error": "No IV snapshot points",
            "symbol": symbol.strip().upper(),
            "trade_date": trade_date.isoformat(),
        }
    history = fetch_atm_iv_history(conn, symbol, trade_date)
    fitted = fit_iv_surface(by_exp, spot, history_ivs=history)
    now = datetime.now(timezone.utc)
    sym = symbol.strip().upper()
    rows: list[tuple[Any, ...]] = []
    for smile in fitted["smiles"]:
        exp = date.fromisoformat(smile["expiry"])
        rows.append(
            (
                sym,
                trade_date,
                exp,
                spot,
                smile["fit_model"],
                {
                    "params": smile.get("params"),
                    "polynomial": smile.get("polynomial"),
                    "svi": smile.get("svi"),
                },
                [p for p in fitted["surface_points"] if p["expiry"] == smile["expiry"]],
                fitted["vol_cone"],
                smile.get("rmse"),
                smile.get("n_points"),
                now,
            )
        )
    if rows:
        batch_upsert(
            conn,
            "features.option_surface_iv_daily",
            _COLS,
            rows,
            conflict_keys=("symbol", "trade_date", "expiry"),
            update_cols=(
                "spot",
                "fit_model",
                "smile_params",
                "surface_points",
                "vol_cone",
                "rmse",
                "n_points",
                "computed_at",
            ),
            set_fetched_at=False,
        )
    return {
        "ok": True,
        "symbol": sym,
        "trade_date": trade_date.isoformat(),
        **fitted,
        "rows_written": len(rows),
    }
