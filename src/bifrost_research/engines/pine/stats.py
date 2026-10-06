"""Forward returns after a Pine signal, measured so a trader could have had them (0.175.0).

``GET /research/pine/signal-stats`` before 0.175.0 entered on the signal
session's close, charged nothing, counted every overlapping signal as its own
sample, averaged undirected returns, compared against a baseline that included
the signal sessions and weighted big names by their session count, and dropped
any name that stopped trading inside the window. This module is the method that
replaced it (``method.version`` 2). Per horizon ``h`` (trading sessions):

Entry and exit
    A signal on session ``k`` exists only once ``k`` has closed. Entry is the
    open of ``k+1`` (its close when the feed has no open for it); exit is the
    close of ``k+h``. Prices are ``raw_market.stock_daily`` adjusted bars, the
    same series the script ran on. A window the data has not reached yet is
    left out; a window cut short by a delisting exits at the last close (B7,
    ``listing_lineage.listing_ends``), and a renamed company's bars are spliced
    across the handover (``listing_lineage.spliced_bars_sql``).

Direction and cost
    ``return`` is in the signal's direction: ``+(exit/entry - 1)`` for a buy,
    ``-(exit/entry - 1)`` for a sell. ``cost_bps`` is charged once each way, so
    the net return is the gross one minus ``2 * cost_bps / 1e4``. The default
    10 bps a side is conservative for the option universe's stocks at the open
    (half a typical spread plus opening-auction slippage on mid caps; large
    caps cost less). ``win`` is a net return above zero; ``hit`` is a gross
    directional move of at least ``move_threshold``.

Overlap
    Within one company and horizon a signal is counted only if it comes ``h``
    or more sessions after the last one counted (sessions on the SPY calendar),
    so no two counted windows share a session. ``n_raw`` is the count before
    this, ``signal.n`` after it.

Baseline
    The same entry, exit and cost on every session of the same companies on
    which this script/side did *not* fire. ``baseline`` pools those sessions
    (a name with more sessions weighs more); ``baseline_equal_weight`` averages
    the per-company rates so each name counts once.

Confidence
    90% intervals from 1,000 bootstrap draws with a fixed seed. With five or
    more companies the draw resamples companies (cluster bootstrap): signals
    on one name in one regime are not independent, and both the signal and the
    baseline side are recomputed on the drawn names, so the edge interval
    carries both. With fewer names it resamples individual signals against a
    fixed baseline (``ci.method = iid_signal``); under five signals there is no
    interval.

Read-only: ``features.stock_signal_pine_daily``, ``raw_market.stock_daily``,
``raw_market.ticker``. D10 BLOCKED — statistics only.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Sequence

import numpy as np

from bifrost_research.repositories.listing_lineage import (
    in_lineage,
    labels,
    listing_ends,
    live_label,
    spliced_bars_sql,
)
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_PINE_DAILY

METHOD_VERSION = 2
DEFAULT_COST_BPS = 10.0
CI_LEVEL = 0.90
BOOT_DRAWS = 1000
BOOT_SEED = 7
MIN_CLUSTERS = 5


def sample_note(n: int) -> str:
    return "noise" if n < 5 else ("thin" if n < 30 else "ok")


_NOTE_RANK = {"noise": 0, "thin": 1, "ok": 2}


def _day(v: Any) -> date:
    return v.date() if isinstance(v, datetime) else v


def dedupe(dates: Sequence[date], calendar: Sequence[date], h: int) -> list[date]:
    """Signals counted after the cooldown: each at least ``h`` sessions after the last counted.

    Session distance is read on ``calendar`` (ascending); a date between
    sessions counts as the next one.
    """
    kept: list[date] = []
    last: int | None = None
    for d in sorted(set(dates)):
        k = bisect.bisect_left(calendar, d)
        if last is None or k - last >= h:
            kept.append(d)
            last = k
    return kept


@dataclass
class _Agg:
    """Sums for one company at one horizon (signal or baseline side)."""

    n: int = 0
    ret: float = 0.0  # sum of undirected gross returns
    wins: int = 0  # net directional return > 0
    wins_gross: int = 0
    hits: int = 0
    delisted: int = 0

    def add(self, other: "_Agg") -> None:
        self.n += other.n
        self.ret += other.ret
        self.wins += other.wins
        self.wins_gross += other.wins_gross
        self.hits += other.hits
        self.delisted += other.delisted


@dataclass
class _Horizon:
    sig: dict[str, _Agg] = field(default_factory=dict)
    sig_rows: list[tuple[str, float, int, int, int]] = field(default_factory=list)  # (sym, ret, win, win_g, hit)
    base: dict[str, _Agg] = field(default_factory=dict)
    n_raw: int = 0


def _stats_sql(horizons: Sequence[int], label_expr: str, keep_clause: str) -> str:
    leads = ",\n".join(f"LEAD(close, {h}) OVER w AS c{h}" for h in horizons)

    def r(h: int) -> str:
        exit_px = f"COALESCE(b.c{h}, CASE WHEN b.sym = ANY(%(delisted)s::text[]) THEN b.last_close END)"
        return f"({exit_px} / b.entry - 1)"

    f_cols = ",\n".join(f"{r(h)} AS r{h}, (b.c{h} IS NULL) AS cut{h}" for h in horizons)

    def per_row(h: int) -> str:
        d = f"(%(sign)s * r{h})"
        return (
            f"(r{h} IS NOT NULL)::int::bigint AS n{h}, COALESCE(r{h}, 0)::float8 AS s{h}, "
            f"COALESCE({d} - %(rt)s > 0, FALSE)::int::bigint AS w{h}, COALESCE({d} > 0, FALSE)::int::bigint AS g{h}, "
            f"COALESCE({d} >= %(thr)s, FALSE)::int::bigint AS t{h}, (r{h} IS NOT NULL AND cut{h})::int::bigint AS x{h}"
        )

    def per_sym(h: int) -> str:
        d = f"(%(sign)s * r{h})"
        return (
            f"COUNT(r{h})::bigint AS n{h}, COALESCE(SUM(r{h}), 0)::float8 AS s{h}, "
            f"COUNT(*) FILTER (WHERE {d} - %(rt)s > 0)::bigint AS w{h}, "
            f"COUNT(*) FILTER (WHERE {d} > 0)::bigint AS g{h}, "
            f"COUNT(*) FILTER (WHERE {d} >= %(thr)s)::bigint AS t{h}, "
            f"COUNT(*) FILTER (WHERE r{h} IS NOT NULL AND cut{h})::bigint AS x{h}"
        )

    return f"""
        WITH sig AS (
            SELECT DISTINCT sym, d FROM unnest(%(sig_sym)s::text[], %(sig_d)s::date[]) AS t(sym, d)
        ),
        bars AS (
            SELECT {label_expr} AS sym, bar_date, open, close
            FROM raw_market.stock_daily
            WHERE symbol = ANY(%(tickers)s::text[]) AND {keep_clause}
              AND bar_date BETWEEN %(start)s AND %(bars_end)s AND close > 0
        ),
        b AS (
            SELECT sym, bar_date,
                   COALESCE(NULLIF(LEAD(open, 1) OVER w, 0), LEAD(close, 1) OVER w) AS entry,
                   {leads},
                   LAST_VALUE(close) OVER (w ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS last_close
            FROM bars
            WINDOW w AS (PARTITION BY sym ORDER BY bar_date)
        ),
        f AS (
            SELECT b.sym, b.bar_date, (s.sym IS NOT NULL) AS is_sig, {f_cols}
            FROM b LEFT JOIN sig s ON s.sym = b.sym AND s.d = b.bar_date
            WHERE b.bar_date BETWEEN %(start)s AND %(end)s AND b.entry > 0
        )
        SELECT 's' AS kind, sym, bar_date, {", ".join(per_row(h) for h in horizons)}
        FROM f WHERE is_sig
        UNION ALL
        SELECT 'b' AS kind, sym, NULL::date, {", ".join(per_sym(h) for h in horizons)}
        FROM f WHERE NOT is_sig
        GROUP BY sym
    """


def _rate(num: float, den: float, nd: int = 4) -> float | None:
    return round(num / den, nd) if den else None


def _ci(values: np.ndarray, nd: int) -> list[float] | None:
    vals = values[np.isfinite(values)]
    if vals.size < BOOT_DRAWS // 2:
        return None
    lo, hi = np.quantile(vals, [(1 - CI_LEVEL) / 2, 1 - (1 - CI_LEVEL) / 2])
    return [round(float(lo), nd), round(float(hi), nd)]


def _bootstrap(hz: _Horizon, sign: int, rt: float) -> tuple[dict[str, list[float] | None], str | None]:
    """90% intervals for win rate, net return and both edges (see the module doc)."""
    empty: dict[str, list[float] | None] = {"win_rate": None, "avg_return": None, "win_rate_edge": None, "avg_return_edge": None}
    clusters = [s for s, a in hz.sig.items() if a.n > 0]
    n = sum(hz.sig[s].n for s in clusters)
    if n < 5:
        return empty, None
    rng = np.random.default_rng(BOOT_SEED)
    if len(clusters) >= MIN_CLUSTERS:
        def arr(side: dict[str, _Agg], attr: str) -> np.ndarray:
            return np.array([getattr(side.get(s, _Agg()), attr) for s in clusters], dtype=float)

        sn, sr, sw = arr(hz.sig, "n"), arr(hz.sig, "ret"), arr(hz.sig, "wins")
        bn, br, bw = arr(hz.base, "n"), arr(hz.base, "ret"), arr(hz.base, "wins")
        idx = rng.integers(0, len(clusters), size=(BOOT_DRAWS, len(clusters)))
        tsn, tsr, tsw = sn[idx].sum(1), sr[idx].sum(1), sw[idx].sum(1)
        tbn, tbr, tbw = bn[idx].sum(1), br[idx].sum(1), bw[idx].sum(1)
        with np.errstate(divide="ignore", invalid="ignore"):
            win = tsw / tsn
            avg = sign * tsr / tsn - rt
            bwin = tbw / tbn
            bavg = sign * tbr / tbn - rt
        method = "cluster_bootstrap_symbol"
    else:
        rows = hz.sig_rows
        rets = np.array([sign * r - rt for _s, r, _w, _g, _t in rows], dtype=float)
        wins = np.array([w for _s, _r, w, _g, _t in rows], dtype=float)
        idx = rng.integers(0, len(rows), size=(BOOT_DRAWS, len(rows)))
        win, avg = wins[idx].mean(1), rets[idx].mean(1)
        tb = _Agg()
        for a in hz.base.values():
            tb.add(a)
        bwin = np.full(BOOT_DRAWS, tb.wins / tb.n if tb.n else np.nan)
        bavg = np.full(BOOT_DRAWS, sign * tb.ret / tb.n - rt if tb.n else np.nan)
        method = "iid_signal"
    return {
        "win_rate": _ci(win, 4),
        "avg_return": _ci(avg, 5),
        "win_rate_edge": _ci(win - bwin, 4),
        "avg_return_edge": _ci(avg - bavg, 5),
    }, method


def _measure(agg: _Agg, sign: int, rt: float) -> dict[str, Any]:
    return {
        "n": agg.n,
        "win_rate": _rate(agg.wins, agg.n),
        "hit_rate": _rate(agg.hits, agg.n),
        "avg_return": round(sign * agg.ret / agg.n - rt, 5) if agg.n else None,
        "win_rate_gross": _rate(agg.wins_gross, agg.n),
        "avg_return_gross": round(sign * agg.ret / agg.n, 5) if agg.n else None,
    }


def _summarize(hz: _Horizon, sign: int, rt: float) -> dict[str, Any]:
    sig, base = _Agg(), _Agg()
    for a in hz.sig.values():
        sig.add(a)
    signal_syms = [s for s, a in hz.sig.items() if a.n > 0]
    for s, a in hz.base.items():
        base.add(a)
    s_m, b_m = _measure(sig, sign, rt), _measure(base, sign, rt)
    eq = [(a.wins / a.n, sign * a.ret / a.n - rt) for a in hz.base.values() if a.n > 0]
    ci, ci_method = _bootstrap(hz, sign, rt)

    def diff(a: float | None, b: float | None, nd: int) -> float | None:
        return round(a - b, nd) if a is not None and b is not None else None

    return {
        "signal": s_m,
        "baseline": b_m,
        "baseline_equal_weight": {
            "win_rate": round(sum(w for w, _ in eq) / len(eq), 4) if eq else None,
            "avg_return": round(sum(r for _, r in eq) / len(eq), 5) if eq else None,
            "symbols": len(eq),
        },
        "win_rate_edge": diff(s_m["win_rate"], b_m["win_rate"], 4),
        "avg_return_edge": diff(s_m["avg_return"], b_m["avg_return"], 5),
        "n_raw": hz.n_raw,
        "sample_note": sample_note(sig.n),
        "clusters": len(signal_syms),
        "delisted_exits": sig.delisted,
        "ci90": ci,
        "ci_method": ci_method,
    }


def read_signals(
    conn: Any, script: str, side: str, start: date, end: date, symbols: Sequence[str]
) -> dict[str, list[date]]:
    """Signal sessions per company (live label), across a rename."""
    tickers = sorted({t for s in symbols for t in labels(s)})
    where_sym = "AND symbol = ANY(%(tickers)s::text[])" if tickers else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT DISTINCT UPPER(symbol), trade_date FROM {TABLE_STOCK_SIGNAL_PINE_DAILY}
                WHERE script_id = %(script)s AND side = %(side)s
                  AND trade_date BETWEEN %(start)s AND %(end)s {where_sym}""",
            {"script": script, "side": side, "start": start, "end": end, "tickers": tickers},
        )
        rows = cur.fetchall() or []
    out: dict[str, list[date]] = {}
    for sym, d in rows:
        d = _day(d)
        if not in_lineage(conn, str(sym), d):
            continue
        out.setdefault(live_label(str(sym)), []).append(d)
    return {s: sorted(set(v)) for s, v in out.items()}


def signal_stats(
    conn: Any,
    *,
    script: str,
    side: str,
    symbols: Sequence[str],
    start: date,
    end: date,
    horizons: Sequence[int],
    move_threshold: float,
    cost_bps: float = DEFAULT_COST_BPS,
    today: date | None = None,
) -> dict[str, Any]:
    """The ``by_horizon`` block and its counts for ``GET /research/pine/signal-stats``."""
    today = today or date.today()
    sign = 1 if side == "buy" else -1
    rt = 2.0 * float(cost_bps) / 1e4
    hs = sorted(set(int(h) for h in horizons))
    by_sym = read_signals(conn, script, side, start, end, symbols)
    n_signals = sum(len(v) for v in by_sym.values())
    method = {
        "version": METHOD_VERSION,
        "entry": "next_open",
        "entry_fallback": "next_close",
        "exit": "close_at_h",
        "cost_bps_one_way": float(cost_bps),
        "cooldown": "per_horizon",
        "baseline": "non_signal_sessions",
        "ci": {"level": CI_LEVEL, "draws": BOOT_DRAWS, "seed": BOOT_SEED, "min_clusters": MIN_CLUSTERS},
        "lineage": True,
    }
    hzs = {h: _Horizon() for h in hs}
    if by_sym:
        bars_end = min(today, end + timedelta(days=math.ceil(max(hs) * 1.6) + 10))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bar_date FROM raw_market.stock_daily WHERE symbol = 'SPY' AND bar_date BETWEEN %s AND %s ORDER BY 1",
                (start, bars_end),
            )
            calendar = [_day(r[0]) for r in cur.fetchall() or []]
        kept = {h: {s: set(dedupe(ds, calendar, h)) for s, ds in by_sym.items()} for h in hs}
        companies = sorted(by_sym)
        delisted = listing_ends(conn, companies, as_of=today)
        label_expr, keep_clause, lin_params, tickers = spliced_bars_sql(conn, companies)
        params = {
            **lin_params,
            "sig_sym": [s for s, ds in by_sym.items() for _ in ds],
            "sig_d": [d for ds in by_sym.values() for d in ds],
            "tickers": tickers,
            "delisted": sorted(delisted),
            "start": start,
            "end": end,
            "bars_end": bars_end,
            "sign": sign,
            "rt": rt,
            "thr": float(move_threshold),
        }
        with conn.cursor() as cur:
            cur.execute(_stats_sql(hs, label_expr, keep_clause), params)
            rows = cur.fetchall() or []
        for row in rows:
            kind, sym, d = row[0], str(row[1]), _day(row[2])
            for i, h in enumerate(hs):
                n, s, w, g, t, x = row[3 + 6 * i : 9 + 6 * i]
                agg = _Agg(int(n), float(s), int(w), int(g), int(t), int(x))
                hz = hzs[h]
                if kind == "b":
                    hz.base[sym] = agg
                    continue
                if not agg.n:
                    continue
                hz.n_raw += 1
                if d not in kept[h].get(sym, ()):
                    continue
                hz.sig.setdefault(sym, _Agg()).add(agg)
                hz.sig_rows.append((sym, agg.ret, agg.wins, agg.wins_gross, agg.hits))
    by_h = {str(h): _summarize(hzs[h], sign, rt) for h in hs}
    worst = min((v["sample_note"] for v in by_h.values()), key=lambda k: _NOTE_RANK[k], default="noise")
    return {"signals": n_signals, "sample_note": worst, "method": method, "by_horizon": by_h}


__all__ = ["DEFAULT_COST_BPS", "METHOD_VERSION", "dedupe", "read_signals", "sample_note", "signal_stats"]
