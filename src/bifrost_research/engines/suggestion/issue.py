"""Mechanical sources: the SPY baseline and live simulator configs (design §5, §8).

Each run looks at the last ``CATCH_UP_SESSIONS`` sessions; a source issues on the
sessions its cadence names (``weekly`` = the first session of each ISO week)
unless that suggestion is already written. Legs are picked on the as-of session
with the simulator's own picker (delta solved from the session's prints);
settlement enters at the next session.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date
from typing import Any, Sequence

from bifrost_research.engines.backtest.sim.chain import ChainStore
from bifrost_research.engines.backtest.sim.engine import _open
from bifrost_research.engines.backtest.sim.rules import SimConfig, fill_basis
from bifrost_research.engines.backtest.sim.walk import with_snapshot_fill
from bifrost_research.engines.suggestion import store
from bifrost_research.engines.suggestion.config import (
    BASELINE,
    BASELINE_SYMBOL,
    CATCH_UP_SESSIONS,
    DAILY_CAP,
    DELTA_TOLERANCE,
    MAX_STALE_SESSIONS,
    SIMULATOR_LIVE,
)
from bifrost_research.engines.suggestion.contract import IncompleteSuggestion, Suggestion, issue_key

logger = logging.getLogger(__name__)


def cadence_sessions(sessions: Sequence[date], cadence: str) -> list[date]:
    """Sessions a cadence issues on. ``weekly``: the first session of each ISO week."""
    if cadence == "daily":
        return list(sessions)
    if cadence != "weekly":
        raise ValueError(f"unknown cadence {cadence!r}")
    out: list[date] = []
    prev: date | None = None
    for d in sessions:
        if prev is None or d.isocalendar()[:2] != prev.isocalendar()[:2]:
            out.append(d)
        prev = d
    return out


def _sim_config(spec: dict[str, Any]) -> SimConfig:
    return SimConfig(
        structure=spec["structure"],
        target_dte=int(spec["target_dte"]),
        min_dte=int(spec.get("min_dte", 7)),
        short_delta=float(spec["short_delta"]),
        wing_width_pct=float(spec.get("wing_width_pct", 0.05)),
        profit_take_pct=spec.get("take_profit_pct"),
        stop_loss_mult=spec.get("stop_loss_mult"),
        dte_exit=spec.get("exit_dte"),
        max_stale_sessions=MAX_STALE_SESSIONS,
    )


def build_suggestion(
    chain: ChainStore,
    d: date,
    *,
    source: str,
    spec: dict[str, Any],
    regime: dict[str, Any],
) -> Suggestion | str:
    """The suggestion ``spec`` makes on session ``d``, or why it cannot."""
    cfg = _sim_config(spec)
    pos = _open(chain, d, cfg)
    if isinstance(pos, str):
        return pos
    picked = [abs(lg.entry_delta) for lg in pos.legs if lg.entry_delta is not None]
    if not picked or abs(picked[0] - cfg.short_delta) > DELTA_TOLERANCE:
        # The nearest strike the data has is not the delta the rule names — a
        # 35-delta put issued under a 30-delta rule is a different suggestion.
        return "delta_out_of_band"
    legs = []
    snap_legs = []
    for lg in pos.legs:
        bar = chain.bar(lg.ticker, d)
        legs.append(
            {
                "contract_key": lg.ticker,
                "right": lg.right,
                "side": lg.side,
                "strike": lg.strike,
                "expiry": lg.expiry.isoformat(),
                "ratio": lg.qty,
                "label": lg.label,
                "pick": "by_delta" if lg.entry_delta is not None else "anchored_wing",
            }
        )
        snap_legs.append(
            {
                "contract_key": lg.ticker,
                "close": bar.close if bar else None,
                "vwap": bar.vwap if bar else None,
                "volume": bar.volume if bar else None,
                "mark": round(lg.entry_mark, 4),
                "iv": round(lg.entry_iv, 4) if lg.entry_iv is not None else None,
                "delta": round(lg.entry_delta, 4) if lg.entry_delta is not None else None,
            }
        )
    snapshot = {
        "spot": chain.spot.get(d),
        "legs": snap_legs,
        "picked_dte": (pos.expiry - d).days,
        "selection": {
            "short_delta": cfg.short_delta,
            "target_dte": cfg.target_dte,
            "min_dte": cfg.min_dte,
            "wing_width_pct": cfg.wing_width_pct,
            "price_field": cfg.price_field,
        },
        "margin": round(pos.margin, 2),
        "credit_basis": fill_basis(cfg),
        "regime": regime,
        "entry_rule": "next session's vwap with tiered slippage",
    }
    return Suggestion(
        as_of_session=d,
        source=source,
        source_ref=spec["source_ref"],
        source_version=str(spec["source_version"]),
        symbol=chain.symbol,
        kind="option_structure",
        structure=spec["structure"],
        legs=tuple(legs),
        take_profit_pct=spec.get("take_profit_pct"),
        stop_loss_mult=spec.get("stop_loss_mult"),
        exit_dte=spec.get("exit_dte"),
        expected_credit=round(pos.credit(), 2),
        max_loss=round(pos.max_loss, 2) if pos.max_loss is not None else None,
        snapshot=snapshot,
        rationale=f"mechanical {source}: {spec['structure']} {cfg.short_delta:g} delta ~{cfg.target_dte} DTE ({spec['cadence']})",
    )


def _specs() -> list[tuple[str, dict[str, Any], str]]:
    out: list[tuple[str, dict[str, Any], str]] = [("baseline", BASELINE, BASELINE["symbol"])]
    for spec in SIMULATOR_LIVE:
        for sym in spec["symbols"]:
            out.append(("simulator", spec, str(sym).upper()))
    return out


def run_issue(conn: Any, *, today: date | None = None) -> dict[str, Any]:
    """Issue the mechanical sources' suggestions for recent sessions; idempotent."""
    end = today or date.today()
    calendar = store.recent_sessions(conn, BASELINE_SYMBOL, end)
    window = calendar[-CATCH_UP_SESSIONS:]
    written: list[str] = []
    skipped: dict[str, int] = {}
    per_session: dict[tuple[str, date], int] = {}
    regimes: dict[tuple[str, date], dict[str, Any]] = {}
    chains: dict[tuple[str, date, int], ChainStore] = {}

    plan: list[tuple[str, dict[str, Any], str, date]] = []
    for source, spec, sym in _specs():
        due = set(cadence_sessions(calendar, spec["cadence"]))
        plan.extend((source, spec, sym, d) for d in window if d in due)
    keys = {issue_key(src, spec["source_ref"], str(spec["source_version"]), d, sym) for src, spec, sym, d in plan}
    have = store.existing_issue_keys(conn, keys)

    for source, spec, sym, d in plan:
        key = issue_key(source, spec["source_ref"], str(spec["source_version"]), d, sym)
        if key in have:
            continue
        if per_session.get((source, d), 0) >= DAILY_CAP.get(source, 10):
            skipped["daily_cap"] = skipped.get("daily_cap", 0) + 1
            continue
        ck = (sym, d, int(spec["target_dte"]))
        if ck not in chains:
            chains[ck] = with_snapshot_fill(
                conn, ChainStore.load(conn, sym, d, d, max_dte=int(spec["target_dte"])), d, d
            )
        chain = chains[ck]
        if d not in chain.spot or not chain.chain_on(d, "P"):
            # Not landed yet: a later run inside the catch-up window issues it.
            skipped["not_landed"] = skipped.get("not_landed", 0) + 1
            continue
        regime = {}
        for who in {BASELINE_SYMBOL, sym}:
            rk = (who, d)
            if rk not in regimes:
                regimes[rk] = store.iv_regime(conn, who, d) or {}
            regime[who] = regimes[rk]
        got = build_suggestion(chain, d, source=source, spec=spec, regime=regime)
        if isinstance(got, str):
            skipped[got] = skipped.get(got, 0) + 1
            continue
        earlier = store.earlier_version(conn, source, spec["source_ref"], d, sym, str(spec["source_version"]))
        if earlier:
            got = replace(got, supersedes_id=earlier)
        try:
            if store.insert_suggestion(conn, got):
                written.append(got.suggestion_id)
                per_session[(source, d)] = per_session.get((source, d), 0) + 1
        except IncompleteSuggestion as exc:
            logger.warning("refused %s: %s", key, exc)
            skipped["incomplete"] = skipped.get("incomplete", 0) + 1
    conn.commit()
    return {
        "sessions": [d.isoformat() for d in window],
        "planned": len(plan),
        "already_issued": len(have),
        "written": len(written),
        "suggestion_ids": written,
        "skipped": skipped,
    }


__all__ = ["build_suggestion", "cadence_sessions", "run_issue"]
