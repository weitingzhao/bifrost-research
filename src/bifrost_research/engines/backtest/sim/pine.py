"""What a Pine script tells the simulator beyond its entry signal (P1, S2/S3/B4/B5).

"Pine decides the timing, Bifrost decides the structure" (Owner 2026-10-06):
entries still come from the stored signal (``features.stock_signal_pine_daily``,
opened the session after it), and this module asks the pine-runner, on demand,
for the two other things a script knows:

- **Exits** (``SimConfig.pine_exit``). ``strategy``: the sessions the script's
  own ``strategy()`` closed a position of the entry's direction (a long for a
  ``buy`` entry, a short for ``sell``). ``reverse_plot``: the sessions the
  opposite plot fired (``sell`` closes a ``buy`` entry). ``auto``: ``strategy``
  for a ``strategy()`` script, ``reverse_plot`` for an ``indicator()``.
- **A price level** (``SimConfig.strike_anchor``): a numeric plot, e.g. the
  Supertrend line, that the short strike is placed against.

Timing, so that nothing acts on what was not yet known:

- An exit is acted on the first session that **opens after it was known**. A
  plot fires on its session's close → the next session. A strategy fill at a
  session's open was decided on the previous close → that session. A fill
  inside a session (a stop or limit the bar traded through) → the next session.
  The option position then closes at that session's ``price_field``, like an
  entry the session after its signal.
- The level for an entry on session D is the plot's value on the last session
  before D: known at D's open.

Nothing is stored: the runner computes this per request from the same adjusted
daily bars the signal build uses, with ``HISTORY_DAYS`` of warm-up, and the same
option context when the script reads any (``engines/pine/context.py``, S6).

D10 BLOCKED — historical replay only.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Literal, Mapping, Sequence

PineExitMode = Literal["auto", "strategy", "reverse_plot"]
PINE_EXIT_MODES: tuple[str, ...] = ("auto", "strategy", "reverse_plot")

#: Calendar days of bars sent before the window so indicators have settled.
HISTORY_DAYS = 600
#: Symbols per runner request (the runner caps a request at 100 series).
CHUNK = 25
#: |delta| rails for a strike placed by a Pine level when the request names none.
ANCHOR_MIN_DELTA = 0.05
ANCHOR_MAX_DELTA = 0.40


class PineRunnerUnavailable(RuntimeError):
    """The runner could not be reached or answered with an error for the whole request."""


@dataclass
class PineOverlay:
    """One symbol's Pine exits and level, ready for the session loop."""

    #: Sessions on which a position of that direction is closed (act sessions, sorted).
    exits: dict[str, list[date]] = field(default_factory=lambda: {"long": [], "short": []})
    #: The anchor plot's value per session (None in warm-up or when ``na``).
    level: dict[date, float | None] = field(default_factory=dict)
    _level_days: list[date] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._level_days = sorted(self.level)

    def exit_after(self, direction: str, d: date) -> date | None:
        """The first exit act session strictly after ``d`` for a ``long`` / ``short`` position."""
        days = self.exits.get(direction) or []
        i = bisect.bisect_right(days, d)
        return days[i] if i < len(days) else None

    def level_before(self, d: date) -> float | None:
        """The level known at ``d``'s open: the plot's value on the last session before ``d``."""
        i = bisect.bisect_left(self._level_days, d) - 1
        return self.level[self._level_days[i]] if i >= 0 else None


def direction_of(side: str | None) -> str:
    """The position a signal side opens: ``buy`` → long, ``sell`` → short."""
    return "short" if side == "sell" else "long"


def _next_session(sessions: Sequence[date], d: date) -> date | None:
    i = bisect.bisect_right(sessions, d)
    return sessions[i] if i < len(sessions) else None


def act_session(sessions: Sequence[date], fill_day: date, *, at_open: bool) -> date | None:
    """The session an exit filled on ``fill_day`` is acted on (see module docstring)."""
    if at_open:
        i = bisect.bisect_left(sessions, fill_day)
        return sessions[i] if i < len(sessions) else None
    return _next_session(sessions, fill_day)


def strategy_exits(sessions: Sequence[date], trades: Mapping[str, Any]) -> dict[str, list[date]]:
    """Act sessions of each closed trade's exit, by the trade's direction."""
    out: dict[str, set[date]] = {"long": set(), "short": set()}
    for t in trades.get("closed") or []:
        d = t.get("exit_date")
        if d is None or t.get("direction") not in out:
            continue
        act = act_session(sessions, d, at_open=bool(t.get("exit_at_open")))
        if act is not None:
            out[t["direction"]].add(act)
    return {k: sorted(v) for k, v in out.items()}


def reverse_plot_exits(sessions: Sequence[date], buy: Sequence[date], sell: Sequence[date]) -> dict[str, list[date]]:
    """A long closes the session after a ``sell`` plot; a short the session after a ``buy``."""

    def nxt(days: Sequence[date]) -> list[date]:
        return sorted({n for n in (_next_session(sessions, d) for d in days) if n is not None})

    return {"long": nxt(sell), "short": nxt(buy)}


def overlay_from_result(
    sessions: Sequence[date],
    res: Mapping[str, Any],
    *,
    exit_mode: str | None,
    anchor_plot: str | None,
) -> tuple[PineOverlay, str | None]:
    """Build one symbol's overlay from a runner result; also the exit mode actually used."""
    used: str | None = None
    exits: dict[str, list[date]] = {"long": [], "short": []}
    if exit_mode:
        trades = res.get("trades")
        if exit_mode == "strategy" or (exit_mode == "auto" and trades is not None):
            if trades is None:
                raise ValueError("pine_exit=strategy needs a strategy() script; this one is an indicator()")
            exits, used = strategy_exits(sessions, trades), "strategy"
        else:
            exits, used = reverse_plot_exits(sessions, res.get("buy") or [], res.get("sell") or []), "reverse_plot"
    level: dict[date, float | None] = {}
    if anchor_plot:
        level = dict((res.get("series") or {}).get(anchor_plot) or {})
    return PineOverlay(exits=exits, level=level), used


def load_overlays(
    conn: Any,
    script_id: str,
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    exit_mode: str | None,
    anchor_plot: str | None,
) -> tuple[dict[str, PineOverlay], dict[str, Any]]:
    """Run ``script_id`` over each symbol's bars and build its overlay; also a report for the summary."""
    from bifrost_research.engines.pine import client, context
    from bifrost_research.engines.pine.build import load_bars_many
    from bifrost_research.engines.pine.library import get_script

    script = get_script(conn, script_id)
    if script is None:
        raise ValueError(f"pine script {script_id!r} not found")
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    want_trades = exit_mode in ("auto", "strategy")
    names = context.referenced(script.source)
    overlays: dict[str, PineOverlay] = {}
    used: set[str] = set()
    report: dict[str, Any] = {
        "script": script.id,
        "script_version": script.version,
        "exit_mode": exit_mode,
        "anchor_plot": anchor_plot,
        "context": names,
        "history_days": HISTORY_DAYS,
        "per_symbol": {},
        "errors": {},
        "warnings": {},
    }
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i : i + CHUNK]
        bars = load_bars_many(conn, chunk, start - timedelta(days=HISTORY_DAYS), end)
        if not bars:
            continue
        extra: dict[str, Any] = {}
        if names:
            extra["context"], extra["market"], _ = context.load(conn, names, bars)
        try:
            results = client.run(
                script.source, bars, plots=[anchor_plot] if anchor_plot else None, trades=want_trades, **extra
            )
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 — unreachable, timeout, refused
            raise PineRunnerUnavailable(f"pine-runner: {type(exc).__name__}: {str(exc)[:200]}") from exc
        for sym, res in results.items():
            if res.get("error"):
                report["errors"][sym] = str(res["error"])[:300]
                continue
            sessions = [b["date"] for b in bars.get(sym, [])]
            ov, mode = overlay_from_result(sessions, res, exit_mode=exit_mode, anchor_plot=anchor_plot)
            overlays[sym] = ov
            if mode:
                used.add(mode)
            warns = [w for w in res.get("warnings") or [] if w.get("code") != "not_a_strategy" or exit_mode == "strategy"]
            if warns:
                report["warnings"][sym] = warns
            report["per_symbol"][sym] = {
                "exits_long": sum(1 for d in ov.exits["long"] if start <= d <= end),
                "exits_short": sum(1 for d in ov.exits["short"] if start <= d <= end),
                "level_sessions": sum(1 for d, v in ov.level.items() if v is not None and start <= d <= end),
            }
    report["exit_mode_used"] = sorted(used)[0] if len(used) == 1 else (sorted(used) or None)
    return overlays, report


def check_pine(cfg: Any, structures: Mapping[str, Sequence[Any]]) -> str | None:
    """The Pine script a run's ``pine_exit`` / ``strike_anchor`` read, after validating them."""
    if not cfg.pine_exit and not cfg.strike_anchor:
        return None
    ev = cfg.entry_event or {}
    script = str((ev.get("params") or {}).get("script") or "").strip()
    if ev.get("kind") != "pine_signal" or not script:
        raise ValueError("pine_exit and strike_anchor need entry_event kind pine_signal with params.script")
    if cfg.pine_exit and cfg.pine_exit not in PINE_EXIT_MODES:
        raise ValueError(f"pine_exit must be one of {list(PINE_EXIT_MODES)}")
    if cfg.strike_anchor:
        a = cfg.strike_anchor
        if not str(a.get("plot") or "").strip():
            raise ValueError("strike_anchor needs plot: the title of a numeric plot in the script")
        lo = float(a.get("min_delta", ANCHOR_MIN_DELTA))
        hi = float(a.get("max_delta", ANCHOR_MAX_DELTA))
        if not 0.0 <= lo < hi <= 1.0:
            raise ValueError("strike_anchor needs 0 <= min_delta < max_delta <= 1")
        picked = [leg for leg in structures[cfg.structure] if leg.anchor is None]
        if len(picked) != 1:
            raise ValueError(
                f"strike_anchor places one short strike; {cfg.structure} picks {len(picked)} legs by delta"
            )
    return script


_COMPARE_KEYS = (
    "n_trades",
    "win_rate",
    "total_pnl",
    "avg_pnl",
    "median_pnl",
    "avg_pnl_ci95",
    "avg_days_held",
    "worst_trade",
    "max_drawdown",
    "sharpe_annual",
    "return_on_peak_margin",
    "exit_reasons",
    "skipped_entries",
)
_DELTA_KEYS = ("n_trades", "win_rate", "total_pnl", "avg_pnl", "avg_days_held", "max_drawdown", "sharpe_annual")


def compare_pine_exit(base: Any, pine: Any, seed: int, *, ci: Callable[[Sequence[float], int], Any]) -> dict[str, Any]:
    """The same run managed by premium rules only vs with the Pine exit added.

    Entries come from the same signals with the same strike rule; they can still
    differ where an earlier Pine exit freed a slot under ``max_open_per_symbol``,
    so the paired figure uses only the entries both runs opened.
    """
    a = {k: base.summary.get(k) for k in _COMPARE_KEYS}
    b = {k: pine.summary.get(k) for k in _COMPARE_KEYS}
    delta: dict[str, float] = {}
    for k in _DELTA_KEYS:
        x, y = a.get(k), b.get(k)
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            delta[k] = round(float(y) - float(x), 4)
    key = lambda t: (t["symbol"], t["entry_date"], t["structure"])  # noqa: E731
    by_a: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for t in base.trades:
        by_a.setdefault(key(t), []).append(t)
    diffs: list[float] = []
    changed = 0
    only_pine = 0
    for t in pine.trades:
        match = by_a.get(key(t))
        if not match:
            only_pine += 1
            continue
        u = match.pop(0)
        diffs.append(float(t["pnl"]) - float(u["pnl"]))
        if t["exit_date"] != u["exit_date"] or t["exit_reason"] != u["exit_reason"]:
            changed += 1
    only_base = sum(len(v) for v in by_a.values())
    return {
        "premium_only": a,
        "with_pine_exit": b,
        "delta": delta,
        "paired": {
            "n": len(diffs),
            "exits_changed": changed,
            "avg_pnl_diff": round(statistics.fmean(diffs), 2) if diffs else None,
            "avg_pnl_diff_ci95": ci(diffs, seed),
            "only_premium_only": only_base,
            "only_with_pine_exit": only_pine,
        },
        "note": (
            "same entry signals and strike rule; paired = entries both runs opened, "
            "pnl with the Pine exit minus premium rules only"
        ),
    }


__all__ = [
    "ANCHOR_MAX_DELTA",
    "ANCHOR_MIN_DELTA",
    "CHUNK",
    "HISTORY_DAYS",
    "PINE_EXIT_MODES",
    "PineExitMode",
    "PineOverlay",
    "PineRunnerUnavailable",
    "act_session",
    "check_pine",
    "compare_pine_exit",
    "direction_of",
    "load_overlays",
    "overlay_from_result",
    "reverse_plot_exits",
    "strategy_exits",
]
