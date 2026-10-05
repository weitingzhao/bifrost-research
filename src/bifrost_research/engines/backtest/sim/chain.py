"""One symbol's option chain over a window, in memory.

Loaded once per (symbol, window): the underlying's sessions and as-traded
closes, every standard contract's daily bar, and the 1-month Treasury yield.
Implied vol and delta are solved from a bar's own price with the same Brent
solver the vol engines use, and cached per (contract, session) — not stored:
the IV solver stopped persisting option_daily inversions in 0.111.0 so there is
one path to the number, and this keeps to it.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, Mapping

from bifrost_research.engines.adjusted_contracts import not_adjusted_contract_sql
from bifrost_research.repositories.listing_lineage import live_label, stock_clause

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OptBar:
    ticker: str
    expiry: date
    strike: float
    right: Literal["C", "P"]
    bar_date: date
    close: float
    vwap: float | None
    volume: int | None

    def price(self, field: str = "vwap") -> float:
        if field == "vwap" and self.vwap is not None and self.vwap > 0:
            return float(self.vwap)
        return float(self.close)


def _d(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _col(row: Any, i: int, key: str) -> Any:
    return row.get(key) if isinstance(row, Mapping) else row[i]


class ChainStore:
    def __init__(
        self,
        symbol: str,
        spot: Mapping[date, float],
        bars: list[OptBar],
        rates: Mapping[date, float] | None = None,
    ) -> None:
        self.symbol = symbol
        # Set by the run when the listing was retired: its last close. Positions
        # still open then close there as ``delisted``, not ``end_of_data``.
        self.delisted_on: date | None = None
        self.sessions: list[date] = sorted(spot)
        self.spot: dict[date, float] = dict(spot)
        self._by_day: dict[tuple[date, str], list[OptBar]] = {}
        self._by_ticker: dict[str, dict[date, OptBar]] = {}
        for b in bars:
            self._by_day.setdefault((b.bar_date, b.right), []).append(b)
            self._by_ticker.setdefault(b.ticker, {})[b.bar_date] = b
        self._rate_days = sorted(rates or {})
        self._rates = dict(rates or {})
        self._greeks: dict[tuple[str, date, str], tuple[float, float] | None] = {}

    # -- loading ----------------------------------------------------------------

    @classmethod
    def load(cls, conn: Any, symbol: str, start: date, end: date, *, max_dte: int) -> "ChainStore":
        # Options sit under the ticker the company trades as now (Plugin 0.51.0);
        # the stock closes splice in the old ticker's bars across a rename (B7).
        sym = live_label(symbol)
        clause, sym_params = stock_clause(conn, sym)
        # Positions opened near ``end`` run on toward their expiry; keep the
        # sessions and the bars that far so they can be marked and settled.
        tail = end + timedelta(days=int(max_dte) * 2 + 14)
        spot: dict[date, float] = {}
        bars: list[OptBar] = []
        rates: dict[date, float] = {}
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT bar_date, COALESCE(close_unadjusted, close) AS close_as_traded
                FROM raw_market.stock_daily
                WHERE {clause}
                  AND bar_date BETWEEN %s AND %s
                  AND close > 0
                ORDER BY bar_date
                """,
                (*sym_params, start, tail),
            )
            for r in cur.fetchall() or []:
                d, px = _d(_col(r, 0, "bar_date")), _col(r, 1, "close_as_traded")
                if d is not None and px is not None and float(px) > 0:
                    spot[d] = float(px)
            cur.execute(
                f"""
                SELECT option_ticker, expiry, strike, option_right, bar_date, close, vwap, volume
                FROM raw_market.option_daily
                WHERE underlying = %s
                  AND bar_date BETWEEN %s AND %s
                  AND close > 0
                  AND {not_adjusted_contract_sql("option_ticker")}
                """,
                (sym, start, tail),
            )
            for r in cur.fetchall() or []:
                right = str(_col(r, 3, "option_right") or "").strip().upper()[:1]
                exp, bd = _d(_col(r, 1, "expiry")), _d(_col(r, 4, "bar_date"))
                if right not in ("C", "P") or exp is None or bd is None:
                    continue
                vwap = _col(r, 6, "vwap")
                vol = _col(r, 7, "volume")
                bars.append(
                    OptBar(
                        ticker=str(_col(r, 0, "option_ticker")),
                        expiry=exp,
                        strike=float(_col(r, 2, "strike")),
                        right=right,  # type: ignore[arg-type]
                        bar_date=bd,
                        close=float(_col(r, 5, "close")),
                        vwap=float(vwap) if vwap is not None else None,
                        volume=int(vol) if vol is not None else None,
                    )
                )
            try:
                cur.execute(
                    """
                    SELECT yield_date, COALESCE(yield_1_month, yield_3_month)
                    FROM raw_market.treasury_yield
                    WHERE yield_date BETWEEN %s AND %s
                    """,
                    (start - timedelta(days=14), tail),
                )
                for r in cur.fetchall() or []:
                    d, v = _d(_col(r, 0, "yield_date")), _col(r, 1, "coalesce")
                    if d is not None and v is not None:
                        x = float(v)
                        x = x / 100.0 if x > 1.0 else x
                        if 0.0 <= x < 0.25:
                            rates[d] = x
            except Exception as exc:  # noqa: BLE001 — rates are optional
                logger.info("treasury yields unavailable: %s", str(exc)[:120])
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
        return cls(sym, spot, bars, rates)

    # -- reads ------------------------------------------------------------------

    def chain_on(self, d: date, right: str) -> list[OptBar]:
        return self._by_day.get((d, right), [])

    def bar(self, ticker: str, d: date) -> OptBar | None:
        return self._by_ticker.get(ticker, {}).get(d)

    def spot_on_or_before(self, d: date) -> float | None:
        i = bisect.bisect_right(self.sessions, d) - 1
        return self.spot[self.sessions[i]] if i >= 0 else None

    def rate(self, d: date) -> float:
        i = bisect.bisect_right(self._rate_days, d) - 1
        if i < 0 or (d - self._rate_days[i]).days > 14:
            return 0.0
        return self._rates[self._rate_days[i]]

    def iv_delta(self, b: OptBar, d: date, price_field: str, *, use_rate: bool = True) -> tuple[float, float] | None:
        key = (b.ticker, d, price_field)
        if key in self._greeks:
            return self._greeks[key]
        from bifrost_research.engines.backtest.canonical_pnl import bs_delta
        from bifrost_research.engines.volatility.iv_solver import solve_iv

        out: tuple[float, float] | None = None
        spot = self.spot.get(d)
        if spot:
            t = max((b.expiry - d).days, 1) / 365.0
            r = self.rate(d) if use_rate else 0.0
            iv, status = solve_iv(spot, b.strike, t, b.price(price_field), b.right, rate=r)
            if iv is not None and status == "ok":
                out = (iv, bs_delta(spot, b.strike, t, iv, right=b.right, rate=r))
        self._greeks[key] = out
        return out


__all__ = ["ChainStore", "OptBar"]
