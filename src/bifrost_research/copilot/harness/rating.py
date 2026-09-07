"""Bifrost Rating — one grade a reader gets in a second, from dimensions the run
already measured.

Owner decision (2026-09-07, option A): a letter for the setup, stars for how
much backs it, a verb for what to do, price levels beside it, and an outlook.
Every one of those is a pure function of the candidate's own record — its
selection, price context, invalidation lines, settled track record and the
judges' stances. No model call. A number a reader will trade on has to come
from a rule they can check, and only a rule can be backtested by
``candidate_outcome`` grouped on (grade, conviction).

The judges still matter: their stances feed conviction and action, and their
prose is the reason shown beside the numbers. They just never set a price.

Parameters are IBD's buy zone and Minervini's stop, as the Owner accepted them:
entry zone = pivot to pivot × 1.05, stop no wider than 8 %, target 2R.

D10: advisory only. An "action" here is a label on a research draft.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

BUY_ZONE_PCT = 0.05
STOP_CAP_PCT = 0.08
TARGET_R = 2.0
STRETCH_R = 3.0
#: Below this many settled outcomes the track record cannot back a call.
MIN_SETTLED = 5
#: Score drift smaller than this is noise between two runs.
OUTLOOK_DELTA = 3.0
#: Within this much below the pivot the name is "in the zone" — close enough
#: that a breakout is the next candle, not a hope.
ZONE_BELOW_PCT = 0.03

RATING_VERSION = 1

ACTION_LABELS: dict[str, str] = {
    "buy_zone": "Buy zone",
    "extended": "Extended — wait for pullback",
    "accumulate": "Accumulate on strength",
    "watch": "Watch",
    "hold_no_add": "Hold — no add",
    "avoid": "Avoid",
}

_SEVERITY = {"oppose": 3, "caution": 2, "abstain": 1, "support": 0}


def _mapping(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _stance(v: Any) -> str:
    s = str(v or "").strip().lower()
    return s if s in _SEVERITY else "abstain"


def most_severe_stance(verdicts: list[dict[str, Any]], agent: str) -> str:
    """Across every judge's row for one persona, the harshest view.

    Two judges give two ``validate`` rows. Taking the harsher is the same rule
    the leash uses, so the rating cannot read a candidate more kindly than the
    gate that decides whether it is accepted.
    """
    seen = [
        _stance(v.get("stance"))
        for v in verdicts
        if isinstance(v, dict) and str(v.get("agent") or "").lower() == agent
    ]
    if not seen:
        return "abstain"
    return max(seen, key=lambda s: _SEVERITY[s])


def settled_record(track: dict[str, Any]) -> tuple[float | None, int]:
    """(hit_rate, judged) at the longest judged horizon, or (None, 0)."""
    horizons = track.get("horizons") if isinstance(track.get("horizons"), list) else []
    best: dict[str, Any] | None = None
    for h in horizons:
        if not isinstance(h, dict):
            continue
        judged = int(_num(h.get("judged")) or 0)
        if judged <= 0:
            continue
        if best is None or int(_num(h.get("horizon_days")) or 0) > int(_num(best.get("horizon_days")) or 0):
            best = h
    if best is None:
        return None, 0
    return _num(best.get("hit_rate")), int(_num(best.get("judged")) or 0)


def conviction_for(
    *,
    agreement: str | None,
    net: str,
    validate: str,
    portfolio: str,
    blocked: bool,
    hit_rate: float | None,
    judged: int,
) -> tuple[int, str]:
    """Stars 1–5 and the rule that set them, evidence before prose.

    Applied in order; the first rule that fires decides. This is Morningstar's
    Uncertainty turned the right way up: the stars say how far to trust the
    grade, and a settled record outranks anything a judge wrote.
    """
    if blocked or validate == "oppose":
        return 1, "validate opposed"
    if agreement == "dissent":
        return 2, "judges dissent"
    if net == "abstain":
        return 2, "judges abstained"
    if validate == "abstain":
        return 2, "no settled record to validate"
    if judged < MIN_SETTLED:
        return 2, f"settled record too thin ({judged} of {MIN_SETTLED})"
    if agreement is None:
        # One judge, or the heuristic. It can carry a setup to three stars but
        # not further: agreement is what four and five stars are made of.
        return 3, "single judge"
    if validate == "caution":
        return 3, "judges agree, validate cautious"
    if validate == "support" and (hit_rate or 0.0) >= 0.5:
        if net == "support" and portfolio != "oppose":
            return 5, "judges agree, validated, record ≥ 0.5, book has room"
        return 4, "judges agree, validated, record ≥ 0.5"
    if validate == "support":
        return 3, f"validated but record {hit_rate:.2f} below 0.5" if hit_rate is not None else "validated, no rate"
    return 3, "judges agree"


def timing_for(close: float | None, pivot: float | None, sma50: float | None) -> dict[str, Any]:
    out: dict[str, Any] = {"pct_vs_pivot": None, "pct_vs_50d": None, "zone": "unknown", "above_50d": None}
    if close is None or close <= 0:
        return out
    if pivot and pivot > 0:
        pct = (close / pivot - 1.0) * 100.0
        out["pct_vs_pivot"] = round(pct, 2)
        if pct > BUY_ZONE_PCT * 100:
            out["zone"] = "extended"
        elif pct >= -ZONE_BELOW_PCT * 100:
            out["zone"] = "in_zone"
        else:
            out["zone"] = "below_pivot"
    if sma50 and sma50 > 0:
        out["pct_vs_50d"] = round((close / sma50 - 1.0) * 100.0, 2)
        out["above_50d"] = close > sma50
    return out


def levels_for(pivot: float | None, sma50: float | None) -> dict[str, Any] | None:
    """Entry zone, stop, targets. None when the run has no pivot to work from."""
    if not pivot or pivot <= 0:
        return None
    entry_lo = pivot
    entry_hi = pivot * (1.0 + BUY_ZONE_PCT)
    cap = pivot * (1.0 - STOP_CAP_PCT)
    if sma50 and sma50 > cap and sma50 < entry_lo:
        stop, source = sma50, "50d"
    else:
        stop, source = cap, f"{int(STOP_CAP_PCT * 100)}% cap"
    risk = entry_lo - stop
    if risk <= 0:
        return None
    return {
        "pivot": round(pivot, 2),
        "entry_lo": round(entry_lo, 2),
        "entry_hi": round(entry_hi, 2),
        "stop": round(stop, 2),
        "stop_source": source,
        "risk_pct": round(risk / entry_lo * 100.0, 2),
        "target_2r": round(entry_lo + TARGET_R * risk, 2),
        "target_3r": round(entry_lo + STRETCH_R * risk, 2),
        "rr": TARGET_R,
    }


def action_for(
    *,
    grade: str | None,
    stage: str | None,
    conviction: int,
    zone: str,
    above_50d: bool | None,
    portfolio: str,
    blocked: bool,
    validate: str,
) -> tuple[str, str]:
    stage_u = str(stage or "").upper()
    if blocked or validate == "oppose":
        return "avoid", "validate opposed"
    if grade in ("C", "D", "E"):
        return "avoid", f"grade {grade}"
    if stage_u.startswith("STAGE_4"):
        return "avoid", "Stage 4 downtrend"
    if portfolio == "oppose":
        return "hold_no_add", "portfolio persona opposed adding"
    if conviction <= 2:
        return "watch", "setup is real, backing is not"
    if zone == "in_zone":
        return "buy_zone", "at the pivot with backing"
    if zone == "extended":
        return "extended", "more than 5% above the pivot"
    if zone == "below_pivot" and above_50d:
        return "accumulate", "below the pivot, holding the 50-day"
    return "watch", "below the 50-day"


def outlook_for(score: float | None, prior: float | None) -> tuple[str | None, dict[str, Any] | None]:
    if score is None or prior is None:
        return None, None
    delta = score - prior
    drift = {"from": round(prior, 2), "to": round(score, 2), "delta": round(delta, 2)}
    if delta <= -OUTLOOK_DELTA:
        return "softening", drift
    if delta >= OUTLOOK_DELTA:
        return "improving", drift
    return "stable", drift


_GRID: dict[tuple[str, str], tuple[str, str]] = {
    ("uptrend", "cold"): ("Buy stock · long calls", "Convexity is cheap; do not sell premium into it."),
    ("uptrend", "neutral"): ("Buy stock", "Plain long; options add little either way."),
    ("uptrend", "hot"): ("Buy stock + covered calls · sell puts to enter", "Get paid for the entry."),
    ("base_or_top", "cold"): ("Watch", "No trend to lean on; cheap vol is cheap for a reason."),
    ("base_or_top", "neutral"): ("Watch", ""),
    ("base_or_top", "hot"): ("Sell premium (range)", "Strangle / condor if terrain reads range."),
    ("downtrend", "cold"): ("Avoid", ""),
    ("downtrend", "neutral"): ("Avoid", ""),
    ("downtrend", "hot"): ("Avoid · hedge only", "Puts if held, nothing new."),
}


def instrument_for(stage: str | None, iv_rank: float | None) -> dict[str, Any]:
    """Stage × IV regime → which instrument fits this tape."""
    s = str(stage or "").upper()
    if s.startswith("STAGE_2"):
        row = "uptrend"
    elif s.startswith("STAGE_4"):
        row = "downtrend"
    elif s.startswith(("STAGE_1", "STAGE_3")):
        row = "base_or_top"
    else:
        row = None
    if iv_rank is None:
        col = None
    elif iv_rank <= 40:
        col = "cold"
    elif iv_rank >= 60:
        col = "hot"
    else:
        col = "neutral"
    out: dict[str, Any] = {"stage_row": row, "iv_col": col, "iv_rank": iv_rank}
    if row and col:
        label, note = _GRID[(row, col)]
        out["suggestion"] = label
        out["note"] = note
    else:
        out["suggestion"] = None
        out["note"] = "stage or IV rank not measured"
    return out


def _why(conv_reason: str, act_reason: str, timing: dict[str, Any], inst: dict[str, Any]) -> str:
    parts = [conv_reason]
    if act_reason and act_reason != conv_reason:
        parts.append(act_reason)
    pct = timing.get("pct_vs_pivot")
    if pct is not None:
        parts.append(f"{abs(pct):.1f}% {'above' if pct > 0 else 'below'} the pivot")
    if inst.get("suggestion") and inst.get("iv_col") in ("cold", "hot"):
        parts.append(f"IV rank {inst['iv_rank']:.0f} → {inst['suggestion'].lower()}")
    return "; ".join(p for p in parts if p) + "."


def rate_candidate(item: dict[str, Any], *, prior_score: float | None = None) -> dict[str, Any]:
    """The rating for one proposed candidate, from its own record."""
    ev = _mapping(item.get("evidence"))
    sel = _mapping(ev.get("selection"))
    price = _mapping(ev.get("price_context"))
    opts = _mapping(ev.get("option_analytics"))
    track = _mapping(ev.get("track_record"))
    verdicts = ev.get("agent_verdicts") if isinstance(ev.get("agent_verdicts"), list) else []

    grade = str(sel.get("grade") or "").strip().upper() or None
    score = _num(sel.get("sepa_score"))
    stage = sel.get("stage")
    net = _stance(item.get("net_stance") or ev.get("net_stance"))
    if str(item.get("net_stance") or ev.get("net_stance") or "").lower() == "dissent":
        net = "dissent"
    agreement_raw = item.get("agreement") or ev.get("agreement")
    agreement = str(agreement_raw).lower() if isinstance(agreement_raw, str) and agreement_raw else None
    blocked = bool(item.get("blocked_by_validate") or ev.get("blocked_by_validate"))
    validate = most_severe_stance(verdicts, "validate")
    portfolio = most_severe_stance(verdicts, "portfolio")
    hit_rate, judged = settled_record(track)

    conviction, conv_reason = conviction_for(
        agreement=agreement,
        net=net,
        validate=validate,
        portfolio=portfolio,
        blocked=blocked,
        hit_rate=hit_rate,
        judged=judged,
    )
    close = _num(price.get("close"))
    pivot = _num(price.get("high_52w"))
    sma50 = _num(price.get("sma_50"))
    timing = timing_for(close, pivot, sma50)
    action, act_reason = action_for(
        grade=grade,
        stage=stage,
        conviction=conviction,
        zone=timing["zone"],
        above_50d=timing["above_50d"],
        portfolio=portfolio,
        blocked=blocked,
        validate=validate,
    )
    levels = levels_for(pivot, sma50) if action != "avoid" else None
    outlook, drift = outlook_for(score, prior_score)
    inst = instrument_for(stage, _num(opts.get("iv_rank_1y")))

    return {
        "version": RATING_VERSION,
        "symbol": str(item.get("symbol") or "").upper(),
        "grade": grade,
        "grade_score": round(score, 2) if score is not None else None,
        "stage": stage,
        "conviction": conviction,
        "conviction_reason": conv_reason,
        "action": action,
        "action_label": ACTION_LABELS[action],
        "action_reason": act_reason,
        "levels": levels,
        "timing": timing,
        "outlook": outlook,
        "score_drift": drift,
        "instrument": inst,
        "inputs": {
            "net": net,
            "agreement": agreement,
            "validate": validate,
            "portfolio": portfolio,
            "blocked": blocked,
            "hit_rate": hit_rate,
            "judged": judged,
        },
        "why": _why(conv_reason, act_reason, timing, inst),
    }


def rating_rank_key(r: dict[str, Any]) -> tuple[int, int, int, float]:
    """Sort key, best first: conviction, action, then how near the pivot.

    Within an action a name sitting at its pivot outranks one 10 % below it:
    the reader's question is "what do I look at first", and the breakout that
    could be the next candle comes before the one that needs a month. A name
    already held ranks below a fresh Watch — it is not a new decision.
    """
    order = {"buy_zone": 0, "accumulate": 1, "extended": 2, "watch": 3, "hold_no_add": 4, "avoid": 5}
    zone = {"in_zone": 0, "below_pivot": 1, "extended": 2, "unknown": 3}
    timing = _mapping(r.get("timing"))
    return (
        -int(r.get("conviction") or 0),
        order.get(str(r.get("action")), 9),
        zone.get(str(timing.get("zone")), 3),
        -float(r.get("grade_score") or 0.0),
    )


def prior_scores(
    conn: Any,
    symbols: list[str],
    *,
    objective_id: str | None = None,
    days: int = 30,
) -> dict[str, float]:
    """The most recent earlier candidate score per symbol, for the outlook.

    Scoped to the same objective when one is given. Two objectives score the
    same name on different scales — the IV watch scored NVDA 62 the day the
    stock screen scored it 82 — and a drift computed across them is not an
    outlook, it is a change of ruler.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bifrost_research.repositories import candidate_pool as pool_repo

    out: dict[str, float] = {}
    # Candidate trade_date is a New York session date; "today" must be too, or
    # a run after 20:00 ET would read its own row as a prior.
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    for sym in symbols:
        try:
            rows = pool_repo.list_candidates(conn, status=None, symbol=sym, days=days, limit=20)
        except Exception as exc:  # noqa: BLE001
            # No prior means no outlook for this name, which the rating says.
            logger.debug("prior score lookup failed for %s: %s", sym, exc)
            continue
        for r in rows:
            if str(r.get("trade_date") or "")[:10] >= today:
                continue
            if objective_id:
                ref = _mapping(r.get("source_ref"))
                if str(ref.get("objective_id") or "") != objective_id:
                    continue
            s = _num(r.get("score"))
            if s is not None:
                out[sym.upper()] = s
                break
    return out


def rate_items(items: list[dict[str, Any]], *, priors: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Attach ``rating`` to every item; return the ratings, best first."""
    priors = priors or {}
    ratings: list[dict[str, Any]] = []
    for item in items:
        sym = str(item.get("symbol") or "").upper()
        r = rate_candidate(item, prior_score=priors.get(sym))
        item["rating"] = r
        ratings.append(r)
    ratings.sort(key=rating_rank_key)
    return ratings


def rating_decision(ratings: list[dict[str, Any]]) -> str:
    if not ratings:
        return "nothing to rate"
    counts: dict[str, int] = {}
    for r in ratings:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    best = max((r.get("conviction") or 0) for r in ratings)
    parts = [f"{n} {a}" for a, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return f"best ★{best} · " + " · ".join(parts)


__all__ = [
    "ACTION_LABELS",
    "BUY_ZONE_PCT",
    "RATING_VERSION",
    "STOP_CAP_PCT",
    "TARGET_R",
    "action_for",
    "conviction_for",
    "instrument_for",
    "levels_for",
    "most_severe_stance",
    "outlook_for",
    "prior_scores",
    "rate_candidate",
    "rate_items",
    "rating_decision",
    "rating_rank_key",
    "settled_record",
    "timing_for",
]
