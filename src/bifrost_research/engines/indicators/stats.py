"""How the stock moved after an indicator signal, against every session (signal evaluation).

For each signal session ``t`` and horizon ``h`` the forward return is
``close[t+h] / close[t] - 1``. A signal is right ("win") when that return has
the signal's sign (up signals expect a rise, down signals a fall), and a hit
when it also clears ``move_threshold`` in that direction. The baseline is the
same measures over every session in the window, so a 55% win rate reads
against the stock's own drift rather than against 50%.
"""

from __future__ import annotations

import statistics
from datetime import date
from typing import Any, Mapping, Sequence

from bifrost_research.engines.indicators.compute import get_signal, signal_mask


def _measure(rets: Sequence[float], sign: int, move_threshold: float) -> dict[str, Any]:
    n = len(rets)
    if n == 0:
        return {"n": 0, "win_rate": None, "hit_rate": None, "avg_return": None, "median_return": None}
    signed = [r * sign for r in rets]
    return {
        "n": n,
        "win_rate": round(sum(1 for r in signed if r > 0) / n, 4),
        "hit_rate": round(sum(1 for r in signed if r >= move_threshold) / n, 4),
        "avg_return": round(statistics.fmean(rets), 5),
        "median_return": round(statistics.median(rets), 5),
    }


def forward_stats(
    series: Mapping[str, tuple[Sequence[date], Sequence[float]]],
    signal_id: str,
    params: Mapping[str, Any] | None,
    start: date,
    end: date,
    *,
    horizons: Sequence[int] = (5, 10, 20),
    move_threshold: float = 0.02,
) -> dict[str, Any]:
    """Signal vs baseline forward returns, pooled over symbols and per symbol."""
    spec = get_signal(signal_id)
    p = spec.params(params)
    sign = 1 if spec.direction == "up" else -1
    pooled_sig: dict[int, list[float]] = {h: [] for h in horizons}
    pooled_base: dict[int, list[float]] = {h: [] for h in horizons}
    per_symbol: dict[str, Any] = {}
    recent: list[dict[str, Any]] = []
    for sym, (dates, closes) in series.items():
        mask = signal_mask(closes, spec.id, p)
        sym_sig: dict[int, list[float]] = {h: [] for h in horizons}
        n_signals = 0
        for i, d in enumerate(dates):
            if not (start <= d <= end):
                continue
            if mask[i]:
                n_signals += 1
                row: dict[str, Any] = {"symbol": sym, "date": d.isoformat(), "close": round(closes[i], 4)}
                for h in horizons:
                    if i + h < len(closes):
                        r = closes[i + h] / closes[i] - 1.0
                        sym_sig[h].append(r)
                        pooled_sig[h].append(r)
                        row[f"ret_{h}"] = round(r, 5)
                    else:
                        row[f"ret_{h}"] = None
                recent.append(row)
            for h in horizons:
                if i + h < len(closes):
                    pooled_base[h].append(closes[i + h] / closes[i] - 1.0)
        per_symbol[sym] = {
            "signals": n_signals,
            "by_horizon": {str(h): _measure(sym_sig[h], sign, move_threshold) for h in horizons},
        }
    by_h: dict[str, Any] = {}
    for h in horizons:
        sig = _measure(pooled_sig[h], sign, move_threshold)
        base = _measure(pooled_base[h], sign, move_threshold)
        edge = (
            round(sig["win_rate"] - base["win_rate"], 4)
            if sig["win_rate"] is not None and base["win_rate"] is not None
            else None
        )
        by_h[str(h)] = {"signal": sig, "baseline": base, "win_rate_edge": edge}
    recent.sort(key=lambda r: (r["date"], r["symbol"]), reverse=True)
    n = sum(v["signals"] for v in per_symbol.values())
    return {
        "signal": {"id": spec.id, "label": spec.label, "direction": spec.direction, "params": p},
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "move_threshold": move_threshold,
        "signals": n,
        "sample_note": "noise" if n < 5 else ("thin" if n < 30 else "ok"),
        "by_horizon": by_h,
        "per_symbol": per_symbol,
        "recent": recent[:50],
    }
