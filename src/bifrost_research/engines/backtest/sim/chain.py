"""One symbol's option chain over a window, in memory.

Loaded once per (symbol, window): the underlying's sessions and as-traded
closes, every standard contract's daily bar, and the 1-month Treasury yield.
Implied vol and delta are solved from a bar's own price with the same Brent
solver the vol engines use, and cached per (contract, session) — not stored:
the IV solver stopped persisting option_daily inversions in 0.111.0 so there is
one path to the number, and this keeps to it.

``ChainStore.load`` keeps every bar of the window resident. The simulator uses
``ChainStore.load_windowed`` instead: sessions and rates up front, option bars
one span of sessions at a time, dropped when the walk moves past them. Every
read the session loop makes is for the session it is on, so the result is the
same; the memory is one span's bars, not the window's. SPY over a year is
744k bars, which ``load`` held at a 620 MB peak and OOM-killed research-api's
512Mi container with (2026-10-06).
"""

from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Literal, Mapping

from bifrost_research.engines.adjusted_contracts import not_adjusted_contract_sql
from bifrost_research.repositories.listing_lineage import live_label, stock_clause
from bifrost_research.pricing import RateCurve, bs_delta, load_rate_curve, solve_iv

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OptBar:
    ticker: str
    expiry: date
    strike: float
    right: Literal["C", "P"]
    bar_date: date
    close: float
    vwap: float | None
    volume: int | None
    # ``daily`` (raw_market.option_daily) or ``snapshot`` (the 16:00 ET
    # option_snapshot's day bar, filled in where option_daily has none).
    source: str = "daily"

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


# O:SPY261120P00765000 → expiry 2026-11-20, right P, strike 765.000
_OCC = re.compile(r"^O:[A-Z.]+(\d{6})([CP])(\d{8})$")
_ET = ZoneInfo("America/New_York")


def _parse_occ(ticker: str) -> tuple[date, str, float] | None:
    m = _OCC.match(ticker)
    if not m:
        return None
    ymd, right, strike = m.groups()
    return date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])), right, int(strike) / 1000.0


@dataclass
class _SnapshotFill:
    """Where and how far the lazy 16:00 snapshot fill reaches (see ``attach_snapshot_fill``)."""

    conn: Any
    since: date
    min_dte: int
    max_dte: int
    chain_days: set[date]
    tried: set[tuple[str, date]]
    sessions_queried: int = 0
    bars_added: int = 0


# Calendar days of option bars a windowed store holds at once: about a month
# of sessions, SPY's largest month (2026-03) being 82k bars.
SPAN_DAYS = 31


def _sessions_and_rates(
    conn: Any, symbol: str, start: date, end: date, *, max_dte: int
) -> tuple[str, dict[date, float], RateCurve, date]:
    """(live label, as-traded closes, Treasury curve, tail) for a window.

    Positions opened near ``end`` run on toward their expiry; the tail keeps the
    sessions and the bars that far so they can be marked and settled.
    """
    # Options sit under the ticker the company trades as now (Plugin 0.51.0);
    # the stock closes splice in the old ticker's bars across a rename (B7).
    sym = live_label(symbol)
    clause, sym_params = stock_clause(conn, sym)
    tail = end + timedelta(days=int(max_dte) * 2 + 14)
    spot: dict[date, float] = {}
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
    # One Treasury reader for the backtester and the IV features (TD-110).
    rates = load_rate_curve(conn, start, tail)
    return sym, spot, rates, tail


def _option_bars(conn: Any, sym: str, lo: date, hi: date) -> list[OptBar]:
    """Every standard contract's ``option_daily`` bar for ``sym`` with ``lo <= bar_date <= hi``."""
    bars: list[OptBar] = []
    # One object per distinct ticker / date: a month of SPY repeats each ~20 times.
    same: dict[Any, Any] = {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT option_ticker, expiry, strike, option_right, bar_date, close, vwap, volume
            FROM raw_market.option_daily
            WHERE underlying = %s
              AND bar_date BETWEEN %s AND %s
              AND close > 0
              AND {not_adjusted_contract_sql("option_ticker")}
            """,
            (sym, lo, hi),
        )
        rows = cur.fetchall() or []
    for r in rows:
        right = str(_col(r, 3, "option_right") or "").strip().upper()[:1]
        exp, bd = _d(_col(r, 1, "expiry")), _d(_col(r, 4, "bar_date"))
        if right not in ("C", "P") or exp is None or bd is None:
            continue
        ticker = str(_col(r, 0, "option_ticker"))
        vwap = _col(r, 6, "vwap")
        vol = _col(r, 7, "volume")
        bars.append(
            OptBar(
                ticker=same.setdefault(ticker, ticker),
                expiry=same.setdefault(exp, exp),
                strike=float(_col(r, 2, "strike")),
                right="C" if right == "C" else "P",
                bar_date=same.setdefault(bd, bd),
                close=float(_col(r, 5, "close")),
                vwap=float(vwap) if vwap is not None else None,
                volume=int(vol) if vol is not None else None,
            )
        )
    return bars


@dataclass
class _Span:
    """The bars a windowed store has resident: ``option_daily`` for ``lo``..``hi``."""

    conn: Any
    first: date
    last: date
    days: int
    lo: date | None = None
    hi: date | None = None
    loads: int = 0
    peak_bars: int = 0


class ChainStore:
    def __init__(
        self,
        symbol: str,
        spot: Mapping[date, float],
        bars: list[OptBar],
        rates: RateCurve | Mapping[date, float] | None = None,
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
            self._index(b)
        self._rates = rates if isinstance(rates, RateCurve) else RateCurve(rates or {})
        self._greeks: dict[tuple[str, date, str], tuple[float, float] | None] = {}
        self._fill: _SnapshotFill | None = None
        self._span: _Span | None = None

    # -- loading ----------------------------------------------------------------

    @classmethod
    def load(cls, conn: Any, symbol: str, start: date, end: date, *, max_dte: int) -> "ChainStore":
        sym, spot, rates, tail = _sessions_and_rates(conn, symbol, start, end, max_dte=max_dte)
        return cls(sym, spot, _option_bars(conn, sym, start, tail), rates)

    @classmethod
    def load_windowed(
        cls, conn: Any, symbol: str, start: date, end: date, *, max_dte: int, span_days: int = SPAN_DAYS
    ) -> "ChainStore":
        """Like ``load``, with option bars read ``span_days`` at a time as the walk reaches them."""
        sym, spot, rates, tail = _sessions_and_rates(conn, symbol, start, end, max_dte=max_dte)
        store = cls(sym, spot, [], rates)
        store._span = _Span(conn, start, tail, max(1, int(span_days)))
        return store

    # -- reads ------------------------------------------------------------------

    def chain_on(self, d: date, right: str) -> list[OptBar]:
        self._reach(d)
        self._fill_chain(d)
        return self._by_day.get((d, right), [])

    # -- windowed bars ----------------------------------------------------------

    def _reach(self, d: date) -> None:
        """Make the span holding ``d`` resident, dropping the one before it."""
        sp = self._span
        if sp is None or (sp.lo is not None and sp.hi is not None and sp.lo <= d <= sp.hi):
            return
        self._drop()
        sp.lo, sp.hi = d, min(d + timedelta(days=sp.days - 1), sp.last)
        if d < sp.first or d > sp.last:
            sp.hi = d  # outside the window: nothing to read, as with ``load``
            return
        for b in _option_bars(sp.conn, self.symbol, sp.lo, sp.hi):
            self._index(b)
        sp.loads += 1
        sp.peak_bars = max(sp.peak_bars, self.resident_bars())

    def _drop(self) -> None:
        self._by_day.clear()
        self._by_ticker.clear()
        # Greeks and the snapshot fill's memo are per session; a later walk
        # over the same sessions (the Pine comparison) reads them again.
        self._greeks.clear()
        if self._fill is not None:
            self._fill.chain_days.clear()
            self._fill.tried.clear()

    def _index(self, b: OptBar) -> None:
        self._by_day.setdefault((b.bar_date, b.right), []).append(b)
        self._by_ticker.setdefault(b.ticker, {})[b.bar_date] = b

    def release(self) -> None:
        """Drop a windowed store's resident bars; the next read loads its span again."""
        sp = self._span
        if sp is None:
            return
        self._drop()
        sp.lo = sp.hi = None

    def resident_bars(self) -> int:
        return sum(len(v) for v in self._by_ticker.values())

    def span_stats(self) -> dict[str, Any] | None:
        sp = self._span
        return None if sp is None else {"span_days": sp.days, "loads": sp.loads, "peak_bars": sp.peak_bars}

    # -- 16:00 snapshot fill ----------------------------------------------------

    def attach_snapshot_fill(self, conn: Any, *, min_dte: int, max_dte: int) -> None:
        """Fill sessions ``option_daily`` thinned out from the 16:00 ET option snapshot.

        Since mid-August 2026 ``option_daily`` keeps about ten strikes either
        side of spot per expiry, so a 20-delta put 45 days out is often not in
        it and the delta pick drifts to the nearest strike that is. The
        resident names' 16:00 snapshot carries the whole chain, and its day
        close / vwap / volume equal ``option_daily``'s row wherever both exist
        (the suggestion ledger's fill, 0.174.1). Lazy, so a year-long run reads
        only what it uses: the whole chain (expiries ``min_dte``..``max_dte``
        out) on a session the run opens on, and the held contracts' bars on a
        session it marks them. ``option_daily`` wins wherever it has the bar.
        No-op before the symbol's first snapshot.
        """
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT min(snapshot_ts) FROM raw_market.option_snapshot WHERE underlying = %s",
                    (self.symbol,),
                )
                row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 — no snapshot table reads as no fill
            logger.info("option_snapshot unavailable for %s: %s", self.symbol, str(exc)[:120])
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return
        first = _col(row, 0, "min") if row else None
        if first is None:
            return
        since = first.astimezone(_ET).date() if isinstance(first, datetime) else _d(first)
        if since is None:
            return
        self._fill = _SnapshotFill(conn, since, int(min_dte), int(max_dte), set(), set())

    def fill_stats(self) -> dict[str, Any]:
        f = self._fill
        if f is None:
            return {"active": False, "since": None, "sessions_queried": 0, "bars_added": 0}
        return {
            "active": True,
            "since": f.since.isoformat(),
            "sessions_queried": f.sessions_queried,
            "bars_added": f.bars_added,
        }

    def prefetch(self, d: date, tickers: list[str]) -> None:
        """Make ``bar(t, d)`` see the snapshot's bar for each held ``t`` option_daily lacks."""
        self._reach(d)
        f = self._fill
        if f is None or d < f.since or d in f.chain_days:
            return
        missing = [t for t in tickers if self.bar(t, d) is None and (t, d) not in f.tried]
        if not missing:
            return
        f.tried.update((t, d) for t in missing)
        self._add_snapshot(d, missing)

    def _fill_chain(self, d: date) -> None:
        f = self._fill
        if f is None or d < f.since or d in f.chain_days:
            return
        f.chain_days.add(d)
        self._add_snapshot(d, None)

    def _add_snapshot(self, d: date, tickers: list[str] | None) -> None:
        f = self._fill
        assert f is not None
        lo = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        sql = """
            SELECT option_ticker, snapshot_ts, day_close, day_vwap, day_volume
            FROM raw_market.option_snapshot
            WHERE underlying = %s
              AND snapshot_ts >= %s AND snapshot_ts < %s
              AND (snapshot_ts AT TIME ZONE 'America/New_York')::time >= '16:00'
              AND day_close > 0
        """
        # 16:00–23:59 ET on ``d`` is 20:00 UTC on ``d`` to 05:00 UTC the next day.
        params: list[Any] = [self.symbol, lo, lo + timedelta(hours=36)]
        if tickers is not None:
            sql += " AND option_ticker = ANY(%s)"
            params.append(list(tickers))
        try:
            with f.conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall() or []
        except Exception as exc:  # noqa: BLE001 — a failed fill leaves option_daily's chain
            logger.info("snapshot fill failed for %s %s: %s", self.symbol, d, str(exc)[:120])
            try:
                f.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return
        f.sessions_queried += 1
        spot = self.spot.get(d)
        for r in rows:
            ticker, ts = str(_col(r, 0, "option_ticker")), _col(r, 1, "snapshot_ts")
            day = ts.astimezone(_ET).date() if isinstance(ts, datetime) else _d(ts)
            if day != d or self.bar(ticker, d) is not None:
                continue
            parsed = _parse_occ(ticker)
            if parsed is None:
                continue
            expiry, right, strike = parsed
            if tickers is None:
                dte = (expiry - d).days
                if dte < f.min_dte or dte > f.max_dte:
                    continue
                if spot and abs(strike / spot - 1.0) > 0.5:
                    continue
            vwap, vol = _col(r, 3, "day_vwap"), _col(r, 4, "day_volume")
            b = OptBar(
                ticker=ticker,
                expiry=expiry,
                strike=strike,
                right=right,  # type: ignore[arg-type]
                bar_date=d,
                close=float(_col(r, 2, "day_close")),
                vwap=float(vwap) if vwap is not None else None,
                volume=int(vol) if vol is not None else None,
                source="snapshot",
            )
            self._index(b)
            f.bars_added += 1

    def bar(self, ticker: str, d: date) -> OptBar | None:
        self._reach(d)
        return self._by_ticker.get(ticker, {}).get(d)

    def spot_on_or_before(self, d: date) -> float | None:
        i = bisect.bisect_right(self.sessions, d) - 1
        return self.spot[self.sessions[i]] if i >= 0 else None

    def rate(self, d: date) -> float:
        return self._rates.on_or_before(d)

    def iv_delta(self, b: OptBar, d: date, price_field: str, *, use_rate: bool = True) -> tuple[float, float] | None:
        key = (b.ticker, d, price_field)
        if key in self._greeks:
            return self._greeks[key]
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
