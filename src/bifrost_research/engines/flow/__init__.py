"""Order Flow engine — sentiment + multi-leg scaffolding (Wave 3.4).

Writes ``features.option_flow_sentiment_daily`` and ``features.option_flow_multi_leg_daily``.

## Data-source dependency

Full order-flow / multi-leg detection prefers a **trade tape**
(``market.option_trades`` from Market Data Plugin daily REST ingest).

**Prefer tape when rows exist** (``data_source=option_trades_tape``).
Otherwise fall back to a **best-effort proxy** from:

- ``market.option_snapshot`` day_volume / OI / day_vwap aggregates
- ``market.option_open_interest`` for OI concentration

D10 BLOCKED — read-only analytics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from bifrost_research.db.upsert import batch_upsert

_SENTIMENT_COLS = (
    "symbol",
    "trade_date",
    "call_notional",
    "put_notional",
    "sentiment_score",
    "call_volume",
    "put_volume",
    "call_oi",
    "put_oi",
    "pcr_volume",
    "pcr_oi",
    "expiry_concentration",
    "strike_concentration",
    "data_source",
    "notes",
    "computed_at",
)

_MULTI_LEG_COLS = (
    "symbol",
    "trade_date",
    "cluster_id",
    "strategy_guess",
    "legs",
    "total_notional",
    "confidence",
    "data_source",
    "notes",
    "computed_at",
)

DATA_SOURCE_PROXY = "option_snapshot_aggregates"
DATA_SOURCE_TAPE = "option_trades_tape"

NOTES_PROXY = (
    "Best-effort proxy from option_snapshot/OI aggregates. "
    "Full tape ingest (Polygon trades → market.option_trades) not yet available "
    "for this symbol/date."
)
NOTES_TAPE = (
    "Aggregated from market.option_trades (Polygon REST daily tape). "
    "Buy/sell aggressor classification still best-effort via call/put notional."
)


@dataclass(frozen=True)
class OptionFlowRow:
    expiry: date
    strike: float
    option_right: str
    volume: int
    open_interest: int
    mid_price: float | None = None  # day_vwap or day_close proxy / tape VWAP


def _notional(volume: int, price: float | None, *, multiplier: float = 100.0) -> float:
    if volume <= 0:
        return 0.0
    px = float(price) if price is not None and price > 0 else 0.0
    if px <= 0:
        # Fallback: treat as $1 premium unit so relative call/put still comparable
        px = 1.0
    return float(volume) * px * multiplier


def _herfindahl(weights: Sequence[float]) -> float:
    total = sum(weights)
    if total <= 0:
        return 0.0
    return sum((w / total) ** 2 for w in weights)


def compute_order_sentiment(
    rows: Sequence[OptionFlowRow],
    *,
    data_source: str = DATA_SOURCE_PROXY,
    notes: str = NOTES_PROXY,
) -> dict[str, Any]:
    """Pure compute: call/put notional + concentration + sentiment score.

    sentiment_score in [-100, +100]: positive = call-biased.
    """
    call_notional = 0.0
    put_notional = 0.0
    call_vol = 0
    put_vol = 0
    call_oi = 0
    put_oi = 0
    by_expiry: dict[date, float] = {}
    by_strike: dict[float, float] = {}

    for r in rows:
        right = (r.option_right or "").strip().upper()
        n = _notional(r.volume, r.mid_price)
        if right in ("C", "CALL"):
            call_notional += n
            call_vol += int(r.volume or 0)
            call_oi += int(r.open_interest or 0)
        elif right in ("P", "PUT"):
            put_notional += n
            put_vol += int(r.volume or 0)
            put_oi += int(r.open_interest or 0)
        else:
            continue
        by_expiry[r.expiry] = by_expiry.get(r.expiry, 0.0) + n
        by_strike[r.strike] = by_strike.get(r.strike, 0.0) + n

    total = call_notional + put_notional
    if total > 0:
        sentiment = 100.0 * (call_notional - put_notional) / total
    else:
        sentiment = 0.0

    pcr_vol = (put_vol / call_vol) if call_vol > 0 else None
    pcr_oi = (put_oi / call_oi) if call_oi > 0 else None

    return {
        "call_notional": round(call_notional, 4),
        "put_notional": round(put_notional, 4),
        "sentiment_score": round(sentiment, 4),
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "pcr_volume": round(pcr_vol, 6) if pcr_vol is not None else None,
        "pcr_oi": round(pcr_oi, 6) if pcr_oi is not None else None,
        "expiry_concentration": round(_herfindahl(list(by_expiry.values())), 6),
        "strike_concentration": round(_herfindahl(list(by_strike.values())), 6),
        "data_source": data_source,
        "notes": notes,
    }


def detect_multi_leg_scaffolding(
    rows: Sequence[OptionFlowRow],
    *,
    min_volume: int = 50,
    data_source: str = DATA_SOURCE_PROXY,
) -> list[dict[str, Any]]:
    """Heuristic multi-leg *scaffolding* from same-expiry high-volume strikes.

    Without synchronized multi-leg fills we still cluster same-expiry call+put
    volume at nearby strikes as candidate structures — **low confidence**.
    """
    by_exp: dict[date, list[OptionFlowRow]] = {}
    for r in rows:
        if int(r.volume or 0) < min_volume:
            continue
        by_exp.setdefault(r.expiry, []).append(r)

    clusters: list[dict[str, Any]] = []
    cluster_idx = 0
    tape = data_source == DATA_SOURCE_TAPE
    for exp, legs in sorted(by_exp.items()):
        calls = [r for r in legs if (r.option_right or "").upper() in ("C", "CALL")]
        puts = [r for r in legs if (r.option_right or "").upper() in ("P", "PUT")]
        if not calls or not puts:
            continue
        calls_sorted = sorted(calls, key=lambda r: r.volume, reverse=True)
        puts_sorted = sorted(puts, key=lambda r: r.volume, reverse=True)
        top_c = calls_sorted[0]
        top_p = puts_sorted[0]
        strike_diff = abs(top_c.strike - top_p.strike)
        if strike_diff < 1e-6:
            guess = "straddle_candidate"
        elif strike_diff / max(top_c.strike, 1.0) < 0.05:
            guess = "strangle_candidate"
        else:
            guess = "vertical_or_combo_candidate"

        notional = _notional(top_c.volume, top_c.mid_price) + _notional(
            top_p.volume, top_p.mid_price
        )
        cluster_idx += 1
        clusters.append(
            {
                "cluster_id": f"{exp.isoformat()}-{cluster_idx}",
                "strategy_guess": guess,
                "legs": [
                    {
                        "expiry": exp.isoformat(),
                        "strike": top_c.strike,
                        "right": "C",
                        "volume": top_c.volume,
                        "oi": top_c.open_interest,
                    },
                    {
                        "expiry": exp.isoformat(),
                        "strike": top_p.strike,
                        "right": "P",
                        "volume": top_p.volume,
                        "oi": top_p.open_interest,
                    },
                ],
                "total_notional": round(notional, 4),
                "confidence": 0.35 if tape else 0.25,
                "data_source": data_source,
                "notes": (
                    "Tape-backed scaffolding — not confirmed simultaneous multi-leg fills."
                    if tape
                    else (
                        "Scaffolding only — not confirmed multi-leg fills. "
                        "Prefer market.option_trades when available."
                    )
                ),
            }
        )
    return clusters


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


def _table_exists(conn: Any, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            LIMIT 1
            """,
            (schema, table),
        )
        row = cur.fetchone() if hasattr(cur, "fetchone") else None
    return row is not None


def fetch_tape_flow_rows(conn: Any, symbol: str, trade_date: date) -> list[OptionFlowRow]:
    """Aggregate market.option_trades (+ OI join) into OptionFlowRow list.

    Returns empty list when the table is missing or has no prints for the day.
    """
    if not _table_exists(conn, "raw_market", "option_trades"):
        return []

    cols = (
        "expiry",
        "strike",
        "option_right",
        "volume",
        "open_interest",
        "vwap",
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              t.expiry,
              t.strike,
              t.option_right,
              COALESCE(SUM(t.size), 0)::bigint AS volume,
              COALESCE(MAX(oi.open_interest), 0)::bigint AS open_interest,
              CASE
                WHEN SUM(t.size) FILTER (WHERE t.price IS NOT NULL AND t.price > 0) > 0
                THEN SUM(t.price * t.size) FILTER (WHERE t.price IS NOT NULL AND t.price > 0)
                     / SUM(t.size) FILTER (WHERE t.price IS NOT NULL AND t.price > 0)
                ELSE NULL
              END AS vwap
            FROM raw_market.option_trades t
            LEFT JOIN raw_market.option_open_interest oi
              ON oi.option_ticker = t.option_ticker
             AND oi.trade_date = t.trade_date
            WHERE UPPER(TRIM(t.underlying)) = %s
              AND t.trade_date = %s
            GROUP BY t.expiry, t.strike, t.option_right
            HAVING COALESCE(SUM(t.size), 0) > 0
            ORDER BY t.expiry, t.strike, t.option_right
            """,
            (symbol.strip().upper(), trade_date),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []

    out: list[OptionFlowRow] = []
    for r in raw or []:
        d = _row_to_dict(r, cols)
        exp = _as_date(d.get("expiry"))
        if exp is None:
            continue
        try:
            strike = float(d["strike"])
        except (TypeError, ValueError, KeyError):
            continue
        mid = None
        if d.get("vwap") is not None:
            try:
                mid = float(d["vwap"])
                if mid <= 0:
                    mid = None
            except (TypeError, ValueError):
                mid = None
        out.append(
            OptionFlowRow(
                expiry=exp,
                strike=strike,
                option_right=str(d.get("option_right") or ""),
                volume=int(d.get("volume") or 0),
                open_interest=int(d.get("open_interest") or 0),
                mid_price=mid,
            )
        )
    return out


def fetch_flow_rows(conn: Any, symbol: str, trade_date: date) -> list[OptionFlowRow]:
    """Best-effort rows from snapshot + OI join (proxy path)."""
    cols = (
        "expiry",
        "strike",
        "option_right",
        "day_volume",
        "open_interest",
        "day_vwap",
        "day_close",
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (oi.option_ticker)
              oi.expiry,
              oi.strike,
              oi.option_right,
              COALESCE(snap.day_volume, 0) AS day_volume,
              oi.open_interest,
              snap.day_vwap,
              snap.day_close
            FROM raw_market.option_open_interest oi
            LEFT JOIN LATERAL (
              SELECT s.day_volume, s.day_vwap, s.day_close
              FROM raw_market.option_snapshot s
              WHERE s.option_ticker = oi.option_ticker
                AND DATE(timezone('America/New_York', s.snapshot_ts)) = %s
              ORDER BY s.snapshot_ts DESC
              LIMIT 1
            ) snap ON TRUE
            WHERE UPPER(TRIM(oi.underlying)) = %s
              AND oi.trade_date = %s
            ORDER BY oi.option_ticker
            """,
            (trade_date, symbol.strip().upper(), trade_date),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []

    out: list[OptionFlowRow] = []
    for r in raw or []:
        d = _row_to_dict(r, cols)
        exp = _as_date(d.get("expiry"))
        if exp is None:
            continue
        try:
            strike = float(d["strike"])
        except (TypeError, ValueError, KeyError):
            continue
        mid = None
        for key in ("day_vwap", "day_close"):
            if d.get(key) is not None:
                try:
                    mid = float(d[key])
                    if mid > 0:
                        break
                except (TypeError, ValueError):
                    mid = None
        out.append(
            OptionFlowRow(
                expiry=exp,
                strike=strike,
                option_right=str(d.get("option_right") or ""),
                volume=int(d.get("day_volume") or 0),
                open_interest=int(d.get("open_interest") or 0),
                mid_price=mid,
            )
        )
    return out


def compute_order_flow_for_symbol(
    conn: Any,
    *,
    symbol: str,
    trade_date: date,
) -> dict[str, Any]:
    tape_rows = fetch_tape_flow_rows(conn, symbol, trade_date)
    if tape_rows:
        rows = tape_rows
        data_source = DATA_SOURCE_TAPE
        notes = NOTES_TAPE
    else:
        rows = fetch_flow_rows(conn, symbol, trade_date)
        data_source = DATA_SOURCE_PROXY
        notes = NOTES_PROXY

    if not rows:
        return {
            "ok": False,
            "error": "No option tape or OI/snapshot rows",
            "symbol": symbol.strip().upper(),
            "trade_date": trade_date.isoformat(),
            "data_source_dependency": NOTES_PROXY,
        }

    sentiment = compute_order_sentiment(rows, data_source=data_source, notes=notes)
    clusters = detect_multi_leg_scaffolding(rows, data_source=data_source)
    now = datetime.now(timezone.utc)
    sym = symbol.strip().upper()

    batch_upsert(
        conn,
        "features.option_flow_sentiment_daily",
        _SENTIMENT_COLS,
        [
            (
                sym,
                trade_date,
                sentiment["call_notional"],
                sentiment["put_notional"],
                sentiment["sentiment_score"],
                sentiment["call_volume"],
                sentiment["put_volume"],
                sentiment["call_oi"],
                sentiment["put_oi"],
                sentiment["pcr_volume"],
                sentiment["pcr_oi"],
                sentiment["expiry_concentration"],
                sentiment["strike_concentration"],
                sentiment["data_source"],
                sentiment["notes"],
                now,
            )
        ],
        conflict_keys=("symbol", "trade_date"),
        update_cols=(
            "call_notional",
            "put_notional",
            "sentiment_score",
            "call_volume",
            "put_volume",
            "call_oi",
            "put_oi",
            "pcr_volume",
            "pcr_oi",
            "expiry_concentration",
            "strike_concentration",
            "data_source",
            "notes",
            "computed_at",
        ),
        set_fetched_at=False,
    )

    if clusters:
        ml_rows = [
            (
                sym,
                trade_date,
                c["cluster_id"],
                c["strategy_guess"],
                c["legs"],
                c["total_notional"],
                c["confidence"],
                c["data_source"],
                c["notes"],
                now,
            )
            for c in clusters
        ]
        batch_upsert(
            conn,
            "features.option_flow_multi_leg_daily",
            _MULTI_LEG_COLS,
            ml_rows,
            conflict_keys=("symbol", "trade_date", "cluster_id"),
            update_cols=(
                "strategy_guess",
                "legs",
                "total_notional",
                "confidence",
                "data_source",
                "notes",
                "computed_at",
            ),
            set_fetched_at=False,
        )

    return {
        "ok": True,
        "symbol": sym,
        "trade_date": trade_date.isoformat(),
        "sentiment": sentiment,
        "multi_leg_candidates": clusters,
        "data_source_dependency": notes,
        "data_source": data_source,
    }
