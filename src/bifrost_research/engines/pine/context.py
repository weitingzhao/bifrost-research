"""Option context for Pine scripts (S6, 0.195.0): named daily series read with ``request.security``.

A script reads a series by its name on its own timeframe::

    iv  = request.security("IV_30", timeframe.period, close)
    vrp = request.security("VRP_20", timeframe.period, close)
    far = request.security("EARN_NEXT", timeframe.period, close) > 15

Names carry an underscore (no US ticker does), never a ``:`` (PineTS strips an
``X:`` prefix). ``SYMBOL`` series are the script's own symbol's; ``MARKET``
series are the same for every symbol. A name outside :data:`CATALOG` is an
error, here when a script is saved or run and again in the runner.

Every value is what was known at that session's close: IV30 is that day's
reconstructed ATM IV, IV rank and the VRP percentile rank against the table's own
earlier days, realised vol uses closes up to that day, and the earnings counts
use only the 8-Ks filed by that day (``EARN_NEXT`` is an estimate, never the
print that later happened). ``stock_signal_vrp_daily.fwd_ret_20d`` looks forward
and is never served.

Alignment (Owner 2026-10-06, option A): each series is put on the script's own
sessions; a session the source lacks takes the last value for at most
:data:`FFILL_SESSIONS` sessions, then reads ``na``; before a series starts it is
``na``. A script that reads context writes no signal until :data:`WARMUP_SESSIONS`
sessions after the latest first value among the series it reads
(:func:`warm_from`), the same 100 the price warm-up uses.

Nothing is stored: every series is read from its own table per request.
Measured history (2026-10-06): ``REPORT-pine-s6-context-series-2026-10-06.md``.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from bifrost_research.db.calendar import closed_days
from bifrost_research.engines.volatility.atm_iv import interpolate_iv_at_dte, iv30_from_expiries
from bifrost_research.repositories.earnings_filings import (
    STALE_AFTER_DAYS,
    distinct_prints,
    expected_next,
    speaks_of_results,
    split_releases,
)

#: Sessions a missing day takes the last value for before reading ``na``.
FFILL_SESSIONS = 5
#: Sessions after the latest first value before a context script's signals count.
WARMUP_SESSIONS = 100
#: ``EARN_NEXT`` when the 52-week rule has nothing: the last print plus a quarter.
EARN_FALLBACK_DAYS = 91
#: The back tenor of ``TERM_30_60`` needs an expiry at least this far out (and at most the next).
TERM_BACK_MIN_DTE = 50
TERM_BACK_MAX_DTE = 120

SYMBOL = "symbol"
MARKET = "market"


@dataclass(frozen=True)
class ContextSeries:
    name: str
    kind: str
    unit: str
    description: str
    history_from: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "unit": self.unit,
            "description": self.description,
            "history_from": self.history_from,
            "note": self.note,
            "pine": f'request.security("{self.name}", timeframe.period, close)',
        }


_SERIES = (
    ContextSeries("IV_30", SYMBOL, "vol points", "30-day ATM implied vol (interpolated across expiries)", "2024-09-09"),
    ContextSeries(
        "IV_RANK",
        SYMBOL,
        "0-100",
        "IV30 rank over the last year: (IV − min) / (max − min)",
        "2025-03-11",
        "needs 126 sessions of IV30; a full 252-session year from 2025-09",
    ),
    ContextSeries("IV_PCTL", SYMBOL, "0-100", "IV30 percentile over the last year", "2025-03-11", "as IV_RANK"),
    ContextSeries("VRP_20", SYMBOL, "vol points", "IV30 minus 20-day realised vol", "2024-09-09"),
    ContextSeries("VRP_60", SYMBOL, "vol points", "IV30 minus 60-day realised vol", "2024-09-09"),
    ContextSeries(
        "VRP_PCTL",
        SYMBOL,
        "0-100",
        "VRP_60 percentile over the last 252 sessions",
        "2024-09-10",
        "ranks against fewer sessions until 2025-09",
    ),
    ContextSeries(
        "TERM_30_60",
        SYMBOL,
        "vol points",
        "ATM IV at 60 DTE minus at 30 DTE; above 0 is contango",
        "2024-09-09",
        "about 70% of name-days; 2026-07 to 2026-09-25 the feed has no 50-120 DTE expiry and it reads na",
    ),
    ContextSeries(
        "EARN_LAST",
        SYMBOL,
        "sessions",
        "sessions since the last results 8-K (Item 2.02); 0 on the day it was filed",
        "2024-09-03",
        "649 of 713 names file Item 2.02; ETFs and foreign filers read na",
    ),
    ContextSeries(
        "EARN_NEXT",
        SYMBOL,
        "sessions",
        "sessions until the expected next results 8-K; 0 when due or a few days overdue",
        "2024-09-03",
        "same quarter last year + 52 weeks from 2025-08 (median miss 0 days, 90% within 7); before that, "
        "last print + 91 days (median miss 5, 90% within 21)",
    ),
    ContextSeries("SPY", MARKET, "price", "SPY daily bars: open, high, low, close, volume (adjusted)", "2020-01-02"),
    ContextSeries("SPY_IV_30", MARKET, "vol points", "SPY 30-day ATM implied vol", "2024-09-09"),
    ContextSeries("SPY_IV_RANK", MARKET, "0-100", "SPY IV30 rank over the last year", "2025-03-11", "as IV_RANK"),
)
CATALOG: dict[str, ContextSeries] = {s.name: s for s in _SERIES}

_CALL_RE = re.compile(r"request\.security\s*\(")
_OWN = {"syminfo.tickerid", "syminfo.ticker"}
_SAME_TIMEFRAME = {"timeframe.period", '"D"', "'D'", '"1D"', "'1D'"}
_NAMED_RE = re.compile(r"^[a-z_]+\s*=(?!=)")


def security_calls(source: str) -> list[list[str]]:
    """The argument list of each ``request.security(`` call, split at top-level commas."""
    out: list[list[str]] = []
    for m in _CALL_RE.finditer(source or ""):
        args: list[str] = []
        depth, cur, quote = 0, "", None
        for ch in source[m.end() :]:
            if quote:
                cur += ch
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
            elif ch in "([":
                depth += 1
            elif ch in ")]":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(cur.strip())
                cur = ""
                continue
            cur += ch
        args.append(cur.strip())
        out.append(args)
    return out


def referenced(source: str) -> list[str]:
    """Context names the script reads, in catalog order. ValueError on a call the
    runner would refuse or a name Research does not serve."""
    if re.search(r"request\.(?!security\s*\()[a-z_]+", source or ""):
        raise ValueError("only request.security is supported")
    if "barmerge.lookahead_on" in (source or ""):
        raise ValueError("lookahead_on reads sessions that had not happened yet; leave lookahead off")
    names: set[str] = set()
    for args in security_calls(source):
        named = {a.split("=", 1)[0].strip(): a.split("=", 1)[1].strip() for a in args if _NAMED_RE.match(a)}
        positional = [a for a in args if not _NAMED_RE.match(a)]
        sym = named.get("symbol", positional[0] if positional else None)
        tf = named.get("timeframe", positional[1] if len(positional) > 1 else None)
        if tf not in _SAME_TIMEFRAME:
            raise ValueError(f"request.security reads the script's own timeframe only (timeframe.period), not {tf}")
        if sym in _OWN:
            continue
        m = re.fullmatch(r"""(["'])([^"']*)\1""", sym or "")
        if not m:
            raise ValueError(f'request.security needs a series name in quotes, e.g. "IV_30"; got {sym}')
        if m.group(2) not in CATALOG:
            raise ValueError(f'unknown series "{m.group(2)}"; available: {", ".join(CATALOG)}')
        names.add(m.group(2))
    return [n for n in CATALOG if n in names]


# -- alignment ---------------------------------------------------------------------------


def align(values: Mapping[date, float | None], sessions: Sequence[date], *, limit: int = FFILL_SESSIONS) -> dict[date, float | None]:
    """``values`` on ``sessions``: a missing or null session carries the last value
    for at most ``limit`` sessions, then reads None; None before the first value."""
    out: dict[date, float | None] = {}
    last: float | None = None
    age = 0
    for d in sessions:
        v = values.get(d)
        if v is not None:
            last, age = float(v), 0
        else:
            age += 1
            if age > limit:
                last = None
        out[d] = last
    return out


def warm_from(series: Iterable[Mapping[date, float | None]], sessions: Sequence[date], *, warmup: int = WARMUP_SESSIONS) -> date | None:
    """The first session a signal may be stored: ``warmup`` sessions after the latest
    first value among ``series``. None when a series never has a value, or too late."""
    latest = 0
    for s in series:
        first = next((i for i, d in enumerate(sessions) if s.get(d) is not None), None)
        if first is None:
            return None
        latest = max(latest, first)
    i = latest + warmup
    return sessions[i] if i < len(sessions) else None


# -- readers: {symbol: {date: value}} straight from each table ------------------------------


def _pct(v: Any) -> float | None:
    return None if v is None else round(float(v) * 100.0, 6)


def _as_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _select(conn: Any, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [tuple(r.values()) if isinstance(r, Mapping) else tuple(r) for r in cur.fetchall() or []]


def _iv_table(conn: Any, symbols: Sequence[str], start: date, end: date) -> dict[str, dict[str, dict[date, float | None]]]:
    out: dict[str, dict[str, dict[date, float | None]]] = {"IV_30": {}, "IV_RANK": {}, "IV_PCTL": {}}
    for sym, d, iv, rank, pctl in _select(
        conn,
        """SELECT symbol, trade_date, iv_current, iv_rank_1y, iv_percentile_1y
           FROM features.option_metric_iv_percentile_daily
           WHERE symbol = ANY(%s::text[]) AND trade_date BETWEEN %s AND %s""",
        (list(symbols), start, end),
    ):
        out["IV_30"].setdefault(sym, {})[d] = _pct(iv)
        out["IV_RANK"].setdefault(sym, {})[d] = _as_float(rank)
        out["IV_PCTL"].setdefault(sym, {})[d] = _as_float(pctl)
    return out


def _vrp_table(conn: Any, symbols: Sequence[str], start: date, end: date) -> dict[str, dict[str, dict[date, float | None]]]:
    out: dict[str, dict[str, dict[date, float | None]]] = {"VRP_20": {}, "VRP_60": {}, "VRP_PCTL": {}}
    for sym, d, v20, v60, pctl in _select(
        conn,
        """SELECT symbol, trade_date, vrp_20d, vrp_60d, vrp_pct_252d
           FROM features.stock_signal_vrp_daily
           WHERE symbol = ANY(%s::text[]) AND trade_date BETWEEN %s AND %s""",
        (list(symbols), start, end),
    ):
        out["VRP_20"].setdefault(sym, {})[d] = _pct(v20)
        out["VRP_60"].setdefault(sym, {})[d] = _pct(v60)
        out["VRP_PCTL"].setdefault(sym, {})[d] = _as_float(pctl)
    return out


def term_30_60(trade_date: date, expiry_ivs: Iterable[tuple[date, float]]) -> float | None:
    """ATM IV at 60 DTE minus at 30, in vol points, from one day's (expiry, atm_iv).

    The 30-day leg is IV30 as everywhere else. The 60-day leg is interpolated
    between the expiries either side of 60 DTE, or the nearest when one-sided, and
    exists only when an expiry lies 50-120 DTE out: without one the "back" would
    be a front-month IV and the slope zero by construction.
    """
    pairs = [(e, float(v)) for e, v in expiry_ivs if v is not None]
    front = iv30_from_expiries(trade_date, pairs)
    pts = [((e - trade_date).days, v) for e, v in pairs if 7 <= (e - trade_date).days <= TERM_BACK_MAX_DTE]
    if front is None or not any(d >= TERM_BACK_MIN_DTE for d, _ in pts):
        return None
    back = interpolate_iv_at_dte(pts, target_dte=60)
    return None if back is None else round((back - front) * 100.0, 6)


def _term(conn: Any, symbols: Sequence[str], start: date, end: date) -> dict[str, dict[str, dict[date, float | None]]]:
    by: dict[tuple[str, date], list[tuple[date, float]]] = {}
    for sym, d, exp, iv in _select(
        conn,
        """SELECT symbol, trade_date, expiry, atm_iv
           FROM features.option_metric_atm_iv_daily
           WHERE symbol = ANY(%s::text[]) AND trade_date BETWEEN %s AND %s AND atm_iv IS NOT NULL""",
        (list(symbols), start, end),
    ):
        by.setdefault((sym, d), []).append((exp, iv))
    out: dict[str, dict[date, float | None]] = {}
    for (sym, d), pairs in by.items():
        out.setdefault(sym, {})[d] = term_30_60(d, pairs)
    return {"TERM_30_60": out}


def open_days(lo: date, hi: date, closed: frozenset[date]) -> list[date]:
    """Weekdays in [lo, hi] the exchange is open, oldest first."""
    out, d = [], lo
    while d <= hi:
        if d.weekday() < 5 and d not in closed:
            out.append(d)
        d += timedelta(days=1)
    return out


def _count_sessions(lo: date, hi: date, days: Sequence[date]) -> int:
    """Sessions in (lo, hi] by the calendar ``days``."""
    return bisect.bisect_right(days, hi) - bisect.bisect_right(days, lo)


def earnings_counts(
    filings: Sequence[tuple[date, bool]], sessions: Sequence[date], days: Sequence[date]
) -> tuple[dict[date, float | None], dict[date, float | None]]:
    """``EARN_LAST`` and ``EARN_NEXT`` on each session from the 8-Ks filed by then.

    Point in time: which filings count as results prints is decided with the
    filings on file that day only (``split_releases`` sets a filing aside by the
    release that follows it, so it is re-run as each filing arrives). ``days``
    is the exchange calendar the counts are taken in; it must reach from the
    first filing to past the last estimate.
    """
    filings = sorted(filings)
    filed = [d for d, _ in filings]
    last_out: dict[date, float | None] = {}
    next_out: dict[date, float | None] = {}
    cache: dict[int, list[date]] = {}
    for d in sessions:
        k = bisect.bisect_right(filed, d)
        if k not in cache:
            cache[k] = distinct_prints(split_releases(filings[:k])[0])
        prints = cache[k]
        if not prints:
            last_out[d] = next_out[d] = None
            continue
        last_out[d] = float(_count_sessions(prints[-1], d, days))
        est = expected_next(prints, as_of=d)
        target = date.fromisoformat(est["date"]) if est else prints[-1] + timedelta(days=EARN_FALLBACK_DAYS)
        if (d - target).days > STALE_AFTER_DAYS:
            next_out[d] = None
        else:
            next_out[d] = float(_count_sessions(d, target, days)) if target > d else 0.0
    return last_out, next_out


def _earnings(conn: Any, symbols: Sequence[str], sessions: Mapping[str, Sequence[date]]) -> dict[str, dict[str, dict[date, float | None]]]:
    days = [d for s in sessions.values() for d in s]
    if not days:
        return {"EARN_LAST": {}, "EARN_NEXT": {}}
    hi = max(days)
    filings: dict[str, list[tuple[date, bool]]] = {}
    for sym, d, text in _select(
        conn,
        """SELECT symbol, filing_date, items_text FROM raw_market.sec_8k_filing
           WHERE symbol = ANY(%s::text[]) AND '2.02' = ANY(items) AND filing_date <= %s""",
        (list(symbols), hi),
    ):
        filings.setdefault(sym, []).append((d, speaks_of_results(text)))
    lo = min([min(days)] + [d for f in filings.values() for d, _ in f])
    end = hi + timedelta(days=200)
    calendar = open_days(lo, end, closed_days(conn, lo, end))
    last: dict[str, dict[date, float | None]] = {}
    nxt: dict[str, dict[date, float | None]] = {}
    for sym in symbols:
        last[sym], nxt[sym] = earnings_counts(filings.get(sym, []), sessions.get(sym, []), calendar)
    return {"EARN_LAST": last, "EARN_NEXT": nxt}


_READERS: tuple[tuple[frozenset[str], Callable[..., dict[str, dict[str, dict[date, float | None]]]]], ...] = (
    (frozenset({"IV_30", "IV_RANK", "IV_PCTL"}), _iv_table),
    (frozenset({"VRP_20", "VRP_60", "VRP_PCTL"}), _vrp_table),
    (frozenset({"TERM_30_60"}), _term),
)


def load(
    conn: Any, names: Sequence[str], bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]]
) -> tuple[dict[str, dict[str, dict[date, float | None]]], dict[str, Any], dict[str, date | None]]:
    """Context for each symbol's own sessions.

    Returns ``(per_symbol, market, warm)``: ``per_symbol[sym][name] = {session: value}``
    aligned and carried as the module says; ``market[name]`` is SPY's bars for
    ``SPY`` and ``{session: value}`` on SPY's sessions otherwise; ``warm[sym]`` is
    the first session that symbol's signals may be stored (:func:`warm_from`).
    """
    unknown = [n for n in names if n not in CATALOG]
    if unknown:
        raise ValueError(f"unknown series {', '.join(unknown)}")
    sessions = {s: [b["date"] for b in bars] for s, bars in bars_by_symbol.items() if bars}
    symbols = list(sessions)
    per_symbol: dict[str, dict[str, dict[date, float | None]]] = {s: {} for s in symbols}
    market: dict[str, Any] = {}
    warm: dict[str, date | None] = {}
    if not symbols:
        return per_symbol, market, warm
    start = min(s[0] for s in sessions.values())
    end = max(s[-1] for s in sessions.values())
    want = set(names)

    raw: dict[str, dict[str, dict[date, float | None]]] = {}
    for group, reader in _READERS:
        if want & group:
            raw.update(reader(conn, symbols, start, end))
    if want & {"EARN_LAST", "EARN_NEXT"}:
        raw.update(_earnings(conn, symbols, sessions))
    for sym in symbols:
        for n in names:
            if CATALOG[n].kind == SYMBOL:
                per_symbol[sym][n] = align(raw.get(n, {}).get(sym, {}), sessions[sym])

    market_names = [n for n in names if CATALOG[n].kind == MARKET]
    if market_names:
        from bifrost_research.engines.pine.build import load_bars_many

        spy = load_bars_many(conn, ["SPY"], start, end).get("SPY", [])
        spy_days = [b["date"] for b in spy]
        if "SPY" in want:
            market["SPY"] = spy
        iv = _iv_table(conn, ["SPY"], start, end) if want & {"SPY_IV_30", "SPY_IV_RANK"} else {}
        for n, src in (("SPY_IV_30", "IV_30"), ("SPY_IV_RANK", "IV_RANK")):
            if n in want:
                market[n] = align(iv.get(src, {}).get("SPY", {}), spy_days)

    for sym in symbols:
        series: list[Mapping[date, float | None]] = [per_symbol[sym][n] for n in names if CATALOG[n].kind == SYMBOL]
        for n in market_names:
            series.append({b["date"]: b["close"] for b in market[n]} if n == "SPY" else market[n])
        warm[sym] = warm_from(series, sessions[sym])
    return per_symbol, market, warm


__all__ = [
    "CATALOG",
    "ContextSeries",
    "EARN_FALLBACK_DAYS",
    "FFILL_SESSIONS",
    "MARKET",
    "SYMBOL",
    "WARMUP_SESSIONS",
    "align",
    "earnings_counts",
    "load",
    "open_days",
    "referenced",
    "security_calls",
    "term_30_60",
    "warm_from",
]
