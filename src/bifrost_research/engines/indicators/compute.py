"""Indicator series and crossing signals over a close series.

Every series is a list aligned to the input closes, ``None`` until it has
enough history. A signal fires on session ``t`` when its condition holds at
``t`` and did not at ``t-1`` — it only uses closes up to and including ``t``,
so a backtest entering on the signal session's close looks at nothing it could
not have seen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Mapping, Sequence

Series = list[float | None]


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def ema(closes: Sequence[float], span: int) -> Series:
    """EMA seeded with the SMA of the first ``span`` closes (as barsChartMath.emaSeries)."""
    n = len(closes)
    out: Series = [None] * n
    if span <= 0 or n < span:
        return out
    head = closes[:span]
    if not all(_finite(c) for c in head):
        return out
    alpha = 2.0 / (span + 1)
    e = sum(head) / span
    out[span - 1] = e
    for i in range(span, n):
        c = closes[i]
        if not _finite(c):
            continue
        e = alpha * c + (1 - alpha) * e
        out[i] = e
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    """Wilder RSI (as barsChartMath.rsiSeries)."""
    n = len(closes)
    out: Series = [None] * n
    if period <= 0 or n < period + 1:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gain += ch
        else:
            loss -= ch
    gain /= period
    loss /= period

    def at() -> float:
        if loss == 0:
            return 50.0 if gain == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    out[period] = at()
    for i in range(period + 1, n):
        ch = closes[i] - closes[i - 1]
        gain = (gain * (period - 1) + max(ch, 0.0)) / period
        loss = (loss * (period - 1) + max(-ch, 0.0)) / period
        out[i] = at()
    return out


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, Series]:
    """MACD line, signal line and histogram (as barsChartMath.macdSeries)."""
    n = len(closes)
    ef, es = ema(closes, fast), ema(closes, slow)
    line: Series = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    sig: Series = [None] * n
    first = next((i for i, v in enumerate(line) if v is not None), None)
    if first is not None:
        seg = [v if v is not None else math.nan for v in line[first:]]
        for j, v in enumerate(ema(seg, signal)):
            sig[first + j] = v
    out_line: Series = [None] * n
    out_sig: Series = [None] * n
    hist: Series = [None] * n
    for i in range(n):
        if line[i] is not None and sig[i] is not None:
            out_line[i], out_sig[i] = line[i], sig[i]
            hist[i] = line[i] - sig[i]  # type: ignore[operator]
    return {"macd": out_line, "signal": out_sig, "hist": hist}


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> dict[str, Series]:
    """Bollinger bands with population σ (as barsChartMath.bollingerSeries)."""
    n = len(closes)
    mid: Series = [None] * n
    upper: Series = [None] * n
    lower: Series = [None] * n
    for i in range(period - 1, n):
        win = closes[i - period + 1 : i + 1]
        if not all(_finite(c) for c in win):
            continue
        m = sum(win) / period
        sd = math.sqrt(sum((c - m) ** 2 for c in win) / period)
        mid[i], upper[i], lower[i] = m, m + mult * sd, m - mult * sd
    return {"mid": mid, "upper": upper, "lower": lower}


# -- signals -----------------------------------------------------------------------

Pair = tuple[Series, Series]


@dataclass(frozen=True)
class SignalSpec:
    """A crossing of series ``a`` over series ``b`` (``direction`` up) or under it (down)."""

    id: str
    label: str
    indicator: str
    direction: str  # "up" | "down"
    defaults: Mapping[str, float] = field(default_factory=dict)
    pair: Callable[[Sequence[float], Mapping[str, float]], Pair] = field(repr=False, default=None)  # type: ignore[assignment]

    def params(self, given: Mapping[str, Any] | None) -> dict[str, float]:
        out = dict(self.defaults)
        for k, v in (given or {}).items():
            if k not in self.defaults:
                continue
            try:
                out[k] = float(v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{self.id}: {k} must be a number") from exc
        for k in ("fast", "slow", "signal", "period", "length"):
            if k in out:
                if out[k] < 2 or out[k] > 400 or out[k] != int(out[k]):
                    raise ValueError(f"{self.id}: {k} must be a whole number in 2..400")
                out[k] = int(out[k])
        if "fast" in out and "slow" in out and out["fast"] >= out["slow"]:
            raise ValueError(f"{self.id}: fast must be shorter than slow")
        return out

    def warmup(self, p: Mapping[str, float]) -> int:
        """Sessions of history the signal needs before it can fire."""
        return int(sum(v for k, v in p.items() if k in ("slow", "signal", "period", "length")) or 0) + 2


def _const(n: int, v: float) -> Series:
    return [v] * n


def _macd_vs_signal(c: Sequence[float], p: Mapping[str, float]) -> Pair:
    m = macd(c, int(p["fast"]), int(p["slow"]), int(p["signal"]))
    return m["macd"], m["signal"]


def _macd_vs_zero(c: Sequence[float], p: Mapping[str, float]) -> Pair:
    m = macd(c, int(p["fast"]), int(p["slow"]), int(p["signal"]))
    return m["macd"], _const(len(c), 0.0)


def _rsi_vs_level(c: Sequence[float], p: Mapping[str, float]) -> Pair:
    return rsi(c, int(p["period"])), _const(len(c), float(p["level"]))


def _close_vs_band(band: str) -> Callable[[Sequence[float], Mapping[str, float]], Pair]:
    def f(c: Sequence[float], p: Mapping[str, float]) -> Pair:
        return list(c), bollinger(c, int(p["period"]), float(p["mult"]))[band]

    return f


def _ema_fast_vs_slow(c: Sequence[float], p: Mapping[str, float]) -> Pair:
    return ema(c, int(p["fast"])), ema(c, int(p["slow"]))


def _close_vs_ema(c: Sequence[float], p: Mapping[str, float]) -> Pair:
    return list(c), ema(c, int(p["length"]))


_MACD = {"fast": 12, "slow": 26, "signal": 9}
_BB = {"period": 20, "mult": 2.0}

SIGNALS: dict[str, SignalSpec] = {
    s.id: s
    for s in (
        SignalSpec("macd_cross_up", "MACD crosses above signal", "macd", "up", _MACD, _macd_vs_signal),
        SignalSpec("macd_cross_down", "MACD crosses below signal", "macd", "down", _MACD, _macd_vs_signal),
        SignalSpec("macd_zero_up", "MACD crosses above zero", "macd", "up", _MACD, _macd_vs_zero),
        SignalSpec("macd_zero_down", "MACD crosses below zero", "macd", "down", _MACD, _macd_vs_zero),
        SignalSpec("rsi_cross_up", "RSI crosses up through level", "rsi", "up", {"period": 14, "level": 30.0}, _rsi_vs_level),
        SignalSpec("rsi_cross_down", "RSI crosses down through level", "rsi", "down", {"period": 14, "level": 70.0}, _rsi_vs_level),
        SignalSpec("bb_lower_break", "Close breaks below lower band", "bollinger", "down", _BB, _close_vs_band("lower")),
        SignalSpec("bb_lower_reclaim", "Close reclaims lower band", "bollinger", "up", _BB, _close_vs_band("lower")),
        SignalSpec("bb_upper_break", "Close breaks above upper band", "bollinger", "up", _BB, _close_vs_band("upper")),
        SignalSpec("ema_cross_up", "Fast EMA crosses above slow EMA", "ema", "up", {"fast": 20, "slow": 50}, _ema_fast_vs_slow),
        SignalSpec("ema_cross_down", "Fast EMA crosses below slow EMA", "ema", "down", {"fast": 20, "slow": 50}, _ema_fast_vs_slow),
        SignalSpec("close_ema_cross_up", "Close crosses above EMA", "ema", "up", {"length": 50}, _close_vs_ema),
        SignalSpec("close_ema_cross_down", "Close crosses below EMA", "ema", "down", {"length": 50}, _close_vs_ema),
    )
}


def get_signal(signal_id: str) -> SignalSpec:
    spec = SIGNALS.get(str(signal_id or "").strip())
    if spec is None:
        raise ValueError(f"unknown indicator signal {signal_id!r}; available: {sorted(SIGNALS)}")
    return spec


def signal_mask(closes: Sequence[float], signal_id: str, params: Mapping[str, Any] | None = None) -> list[bool]:
    """True on each session where the signal fires."""
    spec = get_signal(signal_id)
    p = spec.params(params)
    a, b = spec.pair(closes, p)
    out = [False] * len(closes)
    for i in range(1, len(closes)):
        a0, a1, b0, b1 = a[i - 1], a[i], b[i - 1], b[i]
        if a0 is None or a1 is None or b0 is None or b1 is None:
            continue
        if not all(_finite(x) for x in (a0, a1, b0, b1)):
            continue
        if spec.direction == "up":
            out[i] = a0 <= b0 and a1 > b1
        else:
            out[i] = a0 >= b0 and a1 < b1
    return out


def signal_dates(
    dates: Sequence[date], closes: Sequence[float], signal_id: str, params: Mapping[str, Any] | None = None
) -> list[date]:
    return [d for d, hit in zip(dates, signal_mask(closes, signal_id, params)) if hit]


def catalog() -> list[dict[str, Any]]:
    return [
        {"id": s.id, "label": s.label, "indicator": s.indicator, "direction": s.direction, "defaults": dict(s.defaults)}
        for s in SIGNALS.values()
    ]
