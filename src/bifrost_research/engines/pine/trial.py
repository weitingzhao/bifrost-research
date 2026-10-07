"""Try a Pine script on a basket of names before saving it (Owner 2026-10-06, ledger S13).

POST /research/pine/try: the script runs now in the pine-runner over each name's
adjusted daily bars (with the option context it reads), exactly as the nightly
build would run it — same warm-up, same context warm-up — and its ``buy`` /
``sell`` sessions in the window are measured by ``engines/signal_stats.evaluate``,
the method Signal Decay uses (next-open entry, cost, cooldown, cluster
bootstrap, baseline of the same names' other sessions). Nothing is stored.

D10 BLOCKED — statistics only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from bifrost_research.engines.pine import build, client, context
from bifrost_research.engines.signal_stats import DEFAULT_COST_BPS, evaluate

#: Names per try: one runner request (the build sends 50 a request with context).
MAX_SYMBOLS = 50
#: ``resident``: the option universe's resident tier — the Owner's watchlist plus
#: SPY / QQQ / IWM (21 names on 2026-10-06; SPX has no daily bars and is left out).
#: ``liquid50``: the 50 common stocks in the universe with the highest average
#: dollar volume over the last 60 sessions.
DEFAULT_BASKET = "resident"
BASKETS = ("resident", "liquid50")


@dataclass(frozen=True)
class _Script:
    id: str = "try"
    version: int = 0


def basket_symbols(conn: Any, basket: str) -> list[str]:
    if basket not in BASKETS:
        raise ValueError(f"basket must be one of {list(BASKETS)}")
    with conn.cursor() as cur:
        if basket == "resident":
            cur.execute(
                "SELECT symbol FROM research.option_universe WHERE tier = 'resident' AND symbol <> 'SPX' ORDER BY symbol"
            )
        else:
            cur.execute(
                """
                WITH recent AS (
                    SELECT bar_date FROM raw_market.stock_daily WHERE symbol = 'SPY'
                    ORDER BY bar_date DESC LIMIT 60
                ), cs AS (
                    SELECT DISTINCT ON (symbol) symbol, instrument_type FROM raw_market.ticker
                    ORDER BY symbol, updated_at DESC
                )
                SELECT d.symbol
                FROM raw_market.stock_daily d
                JOIN research.option_universe u ON u.symbol = d.symbol
                JOIN cs ON cs.symbol = d.symbol AND cs.instrument_type = 'CS'
                WHERE d.bar_date >= (SELECT MIN(bar_date) FROM recent) AND d.close > 0
                GROUP BY d.symbol
                ORDER BY AVG(d.close * COALESCE(d.volume, 0)) DESC
                LIMIT %s
                """,
                (MAX_SYMBOLS,),
            )
        return [str(r[0]).upper() for r in cur.fetchall() or []][:MAX_SYMBOLS]


def run_trial(
    conn: Any,
    source: str,
    symbols: Sequence[str],
    *,
    start: date,
    end: date,
    horizons: Sequence[int],
    move_threshold: float = 0.02,
    cost_bps: float = DEFAULT_COST_BPS,
) -> dict[str, Any]:
    """Signals per name and the statistics per side. Raises what ``client.run`` raises."""
    names = context.referenced(source)
    syms = [s.strip().upper() for s in symbols if s.strip()][:MAX_SYMBOLS]
    bars = build.load_bars_many(conn, syms, start - timedelta(days=build.HISTORY_DAYS), end)
    extra: dict[str, Any] = {}
    warm: Mapping[str, date | None] | None = None
    if names:
        extra["context"], extra["market"], warm = context.load(conn, names, bars)
    fired = client.run(source, bars, **extra) if bars else {}
    rows, _errors = build.signal_rows(_Script(), bars, fired, None, warmup_bars=build.WARMUP_BARS, context_warm=warm)
    by_side: dict[str, dict[str, list[date]]] = {"buy": {}, "sell": {}}
    for _sid, sym, d, side, _v, _close in rows:
        if start <= d <= end:
            by_side[side].setdefault(sym, []).append(d)
    per_symbol = []
    for sym in syms:
        res = fired.get(sym)
        row: dict[str, Any] = {
            "symbol": sym,
            "buy": len(by_side["buy"].get(sym, [])),
            "sell": len(by_side["sell"].get(sym, [])),
        }
        if sym not in bars:
            row["error"] = "no daily bars in the window"
        elif res and res.get("error"):
            row.update({k: res[k] for k in ("error", "line", "col") if res.get(k) is not None})
        if warm is not None:
            w = warm.get(sym)
            row["counts_from"] = w.isoformat() if w else None
        per_symbol.append(row)
    stats = {
        side: evaluate(
            conn,
            by_side[side],
            sign=1 if side == "buy" else -1,
            start=start,
            end=end,
            horizons=horizons,
            move_threshold=move_threshold,
            cost_bps=cost_bps,
        )
        for side in ("buy", "sell")
    }
    return {"context": names, "symbols": per_symbol, "stats": stats}


__all__ = ["BASKETS", "DEFAULT_BASKET", "MAX_SYMBOLS", "basket_symbols", "run_trial"]
