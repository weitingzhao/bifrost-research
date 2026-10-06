"""Pine signals as a suggestion source (design §8, S4).

Each run reads the pinned scripts' buy / sell rows for the last
``CATCH_UP_SESSIONS`` sessions (never before ``PINE_LIVE["live_from"]``) and
issues one suggestion per signal the issuance rules let through
(``config.PINE_LIVE``): buy → 20-delta short put, sell → 20-delta call credit
spread, legs picked on the signal session with the simulator's picker; the
settlement enters at the next session, like every other source.

``issue_pine`` is the rule engine: it takes the signals, the suggestions the
ledger already holds and two callbacks (build one, write one), so the rules are
tested without a database. ``run_issue_pine`` wires it to Golden Source.

D10 BLOCKED — writes ``research.suggestion`` only; nothing here reaches an order.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import replace
from datetime import date
from typing import Any, Callable, Iterable

from bifrost_research.db.calendar import ny_today
from bifrost_research.engines.backtest.sim.chain import ChainStore
from bifrost_research.engines.backtest.sim.walk import with_snapshot_fill
from bifrost_research.engines.suggestion import store
from bifrost_research.engines.suggestion.config import BASELINE_SYMBOL, CATCH_UP_SESSIONS, PINE_LIVE
from bifrost_research.engines.suggestion.contract import IncompleteSuggestion, Suggestion
from bifrost_research.engines.suggestion.issue import build_suggestion

logger = logging.getLogger(__name__)

SOURCE = "pine"


def side_of(structure: str | None, cfg: dict[str, Any] = PINE_LIVE) -> str | None:
    """The signal side a Pine suggestion's structure expresses."""
    return next((side for side, m in cfg["sides"].items() if m["structure"] == structure), None)


def rank(d: date, script_id: str, side: str, symbol: str) -> str:
    """Order inside a session's cap: a hash, so no symbol, letter or size is favoured."""
    return hashlib.sha256(f"{d.isoformat()}:{script_id}:{side}:{symbol}".encode()).hexdigest()


def spec_for(script_id: str, version: int, side: str, cfg: dict[str, Any] = PINE_LIVE) -> dict[str, Any]:
    """The ``build_suggestion`` spec for one script and side."""
    m = cfg["sides"][side]
    return {
        "source_ref": script_id,
        "source_version": str(version),
        "structure": m["structure"],
        "short_delta": m["short_delta"],
        "target_dte": cfg["target_dte"],
        "min_dte": cfg["min_dte"],
        "wing_width_pct": cfg["wing_width_pct"],
        "take_profit_pct": cfg["take_profit_pct"],
        "stop_loss_mult": cfg["stop_loss_mult"],
        "exit_dte": cfg["exit_dte"],
        "cadence": f"pine {side} signal",
    }


def issue_pine(
    signals: Iterable[dict[str, Any]],
    issued: Iterable[dict[str, Any]],
    *,
    build: Callable[[dict[str, Any]], Suggestion | str],
    write: Callable[[Suggestion], bool],
    cfg: dict[str, Any] = PINE_LIVE,
) -> dict[str, Any]:
    """Apply the issuance rules to ``signals`` and write what passes.

    ``issued``: the ledger's Pine suggestions (``store.pine_issued`` rows), which
    every cap counts alongside what this run writes. ``build`` returns the
    suggestion for a signal or the reason it cannot be made; ``write`` appends it
    and says whether it was new.
    """
    pinned = {str(s["script_id"]): int(s["script_version"]) for s in cfg["scripts"]}
    live_from: date = cfg["live_from"]
    cooldown = int(cfg["cooldown_days"])
    share = float(cfg["max_share_per_symbol"])
    cap = int(cfg["daily_cap_per_side"])

    rows: list[tuple[str, str, date, str | None]] = [
        (str(r["script_id"]), str(r["symbol"]).upper(), r["as_of"], side_of(r.get("structure"), cfg))
        for r in issued
    ]
    have = {(s, sym, d) for s, sym, d, _ in rows}
    skipped: dict[str, int] = {}
    written: list[str] = []

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    todo = [
        s for s in signals if str(s["script_id"]) in pinned and s["trade_date"] is not None and s["trade_date"] >= live_from
    ]
    sides_on: dict[tuple[str, str, date], set[str]] = {}
    for s in todo:
        sides_on.setdefault((s["script_id"], s["symbol"], s["trade_date"]), set()).add(s["side"])
    todo.sort(key=lambda s: (s["trade_date"], s["script_id"], s["side"], rank(s["trade_date"], s["script_id"], s["side"], s["symbol"])))

    for sig in todo:
        script, sym, d, side = str(sig["script_id"]), str(sig["symbol"]).upper(), sig["trade_date"], str(sig["side"])
        if int(sig["script_version"]) != pinned[script]:
            skip("script_version_not_pinned")
            continue
        if side not in cfg["sides"]:
            skip(f"{side}_not_mapped")
            continue
        if len(sides_on[(script, sym, d)]) > 1:
            skip("both_sides_same_session")
            continue
        if (script, sym, d) in have:
            skip("already_issued")
            continue
        mine = [r for r in rows if r[0] == script]
        if any(r[1] == sym and r[2].isocalendar()[:2] == d.isocalendar()[:2] for r in mine):
            skip("iso_week_cap")
            continue
        if any(r[1] == sym and r[3] == side and abs((d - r[2]).days) < cooldown for r in mine):
            skip("cooldown")
            continue
        n_sym = sum(1 for r in mine if r[1] == sym)
        if n_sym + 1 > max(1, math.floor(share * (len(mine) + 1))):
            skip("symbol_share_cap")
            continue
        if sum(1 for r in mine if r[2] == d and r[3] == side) >= cap:
            skip("daily_cap")
            continue
        got = build(sig)
        if isinstance(got, str):
            skip(got)
            continue
        try:
            new = write(got)
        except IncompleteSuggestion as exc:
            logger.warning("refused pine %s %s %s: %s", script, sym, d, exc)
            skip("incomplete")
            continue
        rows.append((script, sym, d, side))
        have.add((script, sym, d))
        if new:
            written.append(got.suggestion_id)
        else:
            skip("already_issued")
    return {"signals": len(todo), "written": len(written), "suggestion_ids": written, "skipped": skipped}


def _builder(conn: Any, cfg: dict[str, Any]) -> Callable[[dict[str, Any]], Suggestion | str]:
    chains: dict[tuple[str, date], ChainStore] = {}
    regimes: dict[tuple[str, date], dict[str, Any]] = {}

    def regime(who: str, d: date) -> dict[str, Any]:
        if (who, d) not in regimes:
            regimes[(who, d)] = store.iv_regime(conn, who, d) or {}
        return regimes[(who, d)]

    def build(sig: dict[str, Any]) -> Suggestion | str:
        script, sym, d, side = sig["script_id"], sig["symbol"], sig["trade_date"], sig["side"]
        version = int(sig["script_version"])
        if (sym, d) not in chains:
            chains[(sym, d)] = with_snapshot_fill(
                conn, ChainStore.load(conn, sym, d, d, max_dte=int(cfg["target_dte"])), d, d
            )
        chain = chains[(sym, d)]
        if d not in chain.spot:
            return "not_landed"
        spec = spec_for(script, version, side, cfg)
        got = build_suggestion(
            chain, d, source=SOURCE, spec=spec, regime={who: regime(who, d) for who in {BASELINE_SYMBOL, sym}}
        )
        if isinstance(got, str):
            return got
        snapshot = {
            **got.snapshot,
            "signal": {
                "script_id": script,
                "script_version": version,
                "side": side,
                "trade_date": d.isoformat(),
                "close": sig.get("close"),
            },
            "issuance": {
                "rule_version": cfg["rule_version"],
                "live_from": cfg["live_from"].isoformat(),
                "cooldown_days": cfg["cooldown_days"],
                "per_symbol_per_iso_week": cfg["per_symbol_per_iso_week"],
                "max_share_per_symbol": cfg["max_share_per_symbol"],
                "daily_cap_per_side": cfg["daily_cap_per_side"],
            },
        }
        earlier = store.earlier_version(conn, SOURCE, script, d, sym, str(version))
        return replace(
            got,
            snapshot=snapshot,
            supersedes_id=earlier,
            rationale=(
                f"pine {script} v{version} {side} on {d.isoformat()} → {spec['structure']} "
                f"{spec['short_delta']:g} delta ~{spec['target_dte']} DTE"
            ),
        )

    return build


def run_issue_pine(conn: Any, *, today: date | None = None, cfg: dict[str, Any] = PINE_LIVE) -> dict[str, Any]:
    """Issue Pine suggestions for recent sessions; idempotent."""
    if cfg.get("paused"):
        return {"paused": True, "rule_version": cfg["rule_version"], "written": 0}
    end = today or ny_today()
    calendar = store.recent_sessions(conn, BASELINE_SYMBOL, end)
    window = [d for d in calendar[-CATCH_UP_SESSIONS:] if d >= cfg["live_from"]]
    if not window:
        return {"sessions": [], "signals": 0, "written": 0, "skipped": {"before_live_from": 1}}
    script_ids = [str(s["script_id"]) for s in cfg["scripts"]]
    signals = store.pine_signals(conn, script_ids, window)
    issued = store.pine_issued(conn, script_ids, cfg["live_from"])
    out = issue_pine(
        signals,
        issued,
        build=_builder(conn, cfg),
        write=lambda s: store.insert_suggestion(conn, s),
        cfg=cfg,
    )
    conn.commit()
    return {"sessions": [d.isoformat() for d in window], "already_in_ledger": len(issued), **out}


__all__ = ["SOURCE", "issue_pine", "rank", "run_issue_pine", "side_of", "spec_for"]
