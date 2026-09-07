"""Daily Brief cards built from the exhibit readers — research-loop-automation C3.

One source: the same ``build_exhibit`` a hub view's verdict strip and Copilot's
``research.exhibit.get`` read. A card carries the exhibit's verdict (band, label
and meaning from the lens registry), its readings, its as-of date and the hub
view that shows the same numbers — so a Brief card and the view it opens cannot
disagree.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from bifrost_research.lenses.exhibit_model import ExhibitResponse, rollback_quietly
from bifrost_research.lenses.exhibits import build_exhibit
from bifrost_research.lenses.registry import LENSES

logger = logging.getLogger(__name__)

LampColor = str  # green | yellow | red | gray

# The lenses the brief reads, in the order the hubs show them.
BRIEF_LENSES: tuple[str, ...] = (
    "terrain_regime",
    "gex_regime",
    "opex_pin",
    "forecast_path",
    "iv_rank",
    "vrp",
    "skew",
    "term_slope",
    "sepa",
    "momentum",
    "order_sentiment",
)

# Brief card key → lens id. The keys are the contract the page and Copilot read.
CARD_LENS: dict[str, str] = {
    "terrain": "terrain_regime",
    "gex": "gex_regime",
    "opex": "opex_pin",
    "forecast": "forecast_path",
    "iv": "iv_rank",
    "vrp": "vrp",
    "skew": "skew",
    "term_slope": "term_slope",
    "sepa": "sepa",
    "momentum": "momentum",
    "sentiment": "order_sentiment",
}

ExhibitBuilder = Callable[[Any, str, str], ExhibitResponse]


def load_exhibits(
    conn: Any,
    symbol: str,
    lenses: tuple[str, ...] = BRIEF_LENSES,
    builder: ExhibitBuilder = build_exhibit,
) -> dict[str, ExhibitResponse]:
    """Every brief lens for the symbol; a reader that fails becomes a missing exhibit with a caveat."""
    out: dict[str, ExhibitResponse] = {}
    for lens in lenses:
        try:
            out[lens] = builder(conn, lens, symbol)
        except Exception as exc:  # noqa: BLE001 — one lens must not sink the brief
            logger.debug("brief exhibit %s failed: %s", lens, exc)
            rollback_quietly(conn)
            out[lens] = ExhibitResponse(
                lens=lens,
                symbol=symbol,
                lens_id=lens,
                freshness="missing",
                caveats=[f"lens failed: {exc}"],
            )
    return out


def has_reading(exh: ExhibitResponse | None) -> bool:
    return exh is not None and exh.freshness != "missing" and bool(exh.readings)


def lamp_for(exh: ExhibitResponse | None, selected_date: str) -> LampColor:
    """The brief lamp from the exhibit: gray = no reading, green = as of the selected date, yellow = older."""
    if not has_reading(exh):
        return "gray"
    assert exh is not None
    as_of = (exh.as_of or "")[:10]
    if as_of and selected_date and as_of == selected_date[:10]:
        return "green"
    return "yellow"


def hub_route(lens_id: str) -> str:
    """Where the same numbers and verdict are shown — the registry's page_route (a hub view since C1)."""
    spec = LENSES.get(lens_id)
    return spec.page_route if spec else "/research"


def _float_or_none(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN guard


def spot_vs_close(spot: float, close: float) -> str:
    """Spot relative to the expected close — the word and the sign describe the same gap."""
    pct = ((spot - close) / max(close, 1.0)) * 100
    dir_word = "above" if pct >= 0 else "below"
    sign = "+" if pct >= 0 else ""
    return f"spot {spot:.2f} {dir_word} E[close] {close:.2f} ({sign}{pct:.2f}%)"


def _fmt_pct(value: Any, digits: int = 1) -> str:
    f = _float_or_none(value)
    return "—" if f is None else f"{f * 100:.{digits}f}%"


def _fmt_signed_pct(value: Any, digits: int = 1) -> str:
    f = _float_or_none(value)
    return "—" if f is None else f"{f * 100:+.{digits}f}%"


def _fmt_num(value: Any, digits: int = 0) -> str:
    f = _float_or_none(value)
    return "—" if f is None else f"{f:.{digits}f}"


def _ordinal_word(value: float) -> str:
    n = int(round(value))
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


# ─── card text, one function per lens ────────────────────────────────────────


def terrain_text(symbol: str, exh: ExhibitResponse | None) -> str:
    if not has_reading(exh):
        return f"No terrain for {symbol}"
    r = exh.readings  # type: ignore[union-attr]
    spot, close = _float_or_none(r.get("spot")), _float_or_none(r.get("expected_close"))
    gap = f" · {spot_vs_close(spot, close)}" if spot is not None and close is not None else ""
    return f"{r.get('regime')} · pin {_fmt_num(r.get('pin_score'))} · tail {_fmt_num(r.get('tail_risk'))}{gap}"


def gex_text(symbol: str, exh: ExhibitResponse | None) -> str:
    if not has_reading(exh):
        return f"No GEX levels for {symbol}"
    r = exh.readings  # type: ignore[union-attr]
    text = (
        f"Spot {_fmt_num(r.get('spot'))} {r.get('spot_vs_zero_gamma') or 'vs'} zero-γ {_fmt_num(r.get('zero_gamma'))}"
        f" · put {_fmt_num(r.get('major_put_wall'))} / call {_fmt_num(r.get('major_call_wall'))}"
    )
    link = r.get("vrp_link")
    if isinstance(link, dict) and _float_or_none(link.get("rv_20d")) is not None and _float_or_none(link.get("atm_iv_30d")) is not None:
        agree = link.get("consistent")
        word = "agrees with" if agree is True else "contradicts" if agree is False else "vs"
        text += f" · RV20 {_fmt_pct(link.get('rv_20d'))} vs IV30 {_fmt_pct(link.get('atm_iv_30d'))} — realised vol {word} the regime"
    return text


def opex_text(symbol: str, exh: ExhibitResponse | None) -> str:
    if not has_reading(exh):
        return f"No OpEx pin reading for {symbol}"
    r = exh.readings  # type: ignore[union-attr]
    strike, close, dist = _float_or_none(r.get("max_pain_strike")), _float_or_none(r.get("close")), _float_or_none(r.get("pin_pct_distance"))
    if strike is None:
        return f"No max-pain strike for {symbol}"
    side = "from" if close is None or close == strike else ("below" if strike < close else "above")
    text = f"Max pain {strike:.0f}"
    if dist is not None:
        text += f" · {abs(dist) * 100:.1f}% {side} spot"
    h = exh.history_summary  # type: ignore[union-attr]
    cycles = int(_float_or_none(h.get("cycles")) or 0)
    if cycles:
        text += f" · pinned {int(_float_or_none(h.get('pinned')) or 0)} of {cycles} cycles"
    else:
        text += " · no settled cycles yet"
    return text


def forecast_text(symbol: str, exh: ExhibitResponse | None, latest_session: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if latest_session:
        close = _float_or_none(latest_session.get("expected_close"))
        parts.append(f"{latest_session.get('regime')} · E[close] {close:.2f}" if close is not None else str(latest_session.get("regime")))
    if has_reading(exh):
        r = exh.readings  # type: ignore[union-attr]
        parts.append(
            f"30d path hit {_fmt_pct(r.get('path_hit_rate'), 0)} across {int(_float_or_none(r.get('session_count')) or 0)} settled"
            f" · avg |miss| {_fmt_pct(r.get('avg_close_miss_pct'))}"
        )
    else:
        parts.append(f"No settled forecast sessions for {symbol} in 30d")
    return " · ".join(parts)


def iv_text(symbol: str, exh: ExhibitResponse | None) -> str:
    if not has_reading(exh):
        return f"No IV row for {symbol}"
    r = exh.readings  # type: ignore[union-attr]
    if r.get("iv_rank_1y") is None and r.get("vrp_pct_252d_proxy") is not None:
        return f"IV Rank missing · VRP pctl {_fmt_num(r.get('vrp_pct_252d_proxy'))} as proxy"
    return f"IV Rank {_fmt_num(r.get('iv_rank_1y'))} · pctl {_fmt_num(r.get('iv_percentile_1y'))} · IV {_fmt_pct(r.get('iv_current'))}"


def vrp_text(symbol: str, exh: ExhibitResponse | None) -> str:
    if not has_reading(exh):
        return f"No VRP row for {symbol}"
    r = exh.readings  # type: ignore[union-attr]
    pct = _float_or_none(r.get("vrp_pct_252d"))
    head = f"VRP {_ordinal_word(pct)} pctl" if pct is not None else "VRP pctl —"
    return f"{head} · IV30 {_fmt_pct(r.get('atm_iv_30d'))} vs RV60 {_fmt_pct(r.get('rv_60d'))} · spread {_fmt_signed_pct(r.get('vrp_60d'))}"


def skew_text(symbol: str, exh: ExhibitResponse | None) -> str:
    if not has_reading(exh):
        return f"No SVI fit for {symbol}"
    r = exh.readings  # type: ignore[union-attr]
    slope = _float_or_none(r.get("atm_slope"))
    if slope is None:
        return f"SVI fit for {symbol} without an ATM slope"
    kind = "call skew" if slope < 0 else "put skew"
    pctile, days = _float_or_none(r.get("slope_pctile_252d")), _float_or_none(r.get("history_days"))
    own = (
        f" · {'bottom' if pctile < 1 else 'top' if pctile > 99 else _ordinal_word(pctile) + ' pctl'} of own year"
        f"{f' ({int(days)}d)' if days is not None else ''}"
        if pctile is not None
        else ""
    )
    return f"ATM slope {slope:+.3f} ({kind}){own} · ATM vol {_fmt_pct(r.get('atm_vol'))}"


def term_text(symbol: str, exh: ExhibitResponse | None) -> str:
    if not has_reading(exh):
        return f"No term structure for {symbol}"
    r = exh.readings  # type: ignore[union-attr]
    back = _float_or_none(r.get("backwardation"))
    label = str(r.get("term_structure") or "term").capitalize()
    head = f"{label} {back * 100:+.1f} pts" if back is not None else label
    near, far = _float_or_none(r.get("near_vol")), _float_or_none(r.get("far_vol"))
    if near is None or far is None:
        return head
    nd, fd = _float_or_none(r.get("near_dte")), _float_or_none(r.get("far_dte"))
    return f"{head} · near {near * 100:.1f}%{f' ({int(nd)}d)' if nd is not None else ''} vs far {far * 100:.1f}%{f' ({int(fd)}d)' if fd is not None else ''}"


def sepa_text(symbol: str, exh: ExhibitResponse | None, setup_count: int, pivot_count: int) -> str:
    market = f"market Setup {setup_count} · Pivot {pivot_count}"
    if not has_reading(exh):
        return f"No SEPA row for {symbol} · {market}"
    r = exh.readings  # type: ignore[union-attr]
    return f"{symbol} {r.get('path') or r.get('stage') or '—'} · grade {r.get('grade') or '—'} · score {_fmt_num(r.get('sepa_score'))} · {market}"


def momentum_text(symbol: str, exh: ExhibitResponse | None, grade_counts: dict[str, int]) -> str:
    market = f"market A+ {grade_counts.get('A+', 0)} · A {grade_counts.get('A', 0)} · B {grade_counts.get('B', 0)}"
    if not has_reading(exh):
        return f"No momentum row for {symbol} · {market}"
    r = exh.readings  # type: ignore[union-attr]
    path = f" · {r.get('path')}" if r.get("path") else ""
    return f"{symbol} {r.get('grade') or '—'} · score {_fmt_num(r.get('score'))}{path} · {market}"


# ─── the card itself ─────────────────────────────────────────────────────────


def exhibit_card(
    exh: ExhibitResponse | None,
    *,
    lens_id: str,
    selected_date: str,
    text: str,
    detail: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A brief card: the exhibit's verdict, readings and hub route, plus the card's own line."""
    verdict = (exh.verdict if exh else None) or {}
    card: dict[str, Any] = {
        "present": has_reading(exh),
        "verdict": text,
        "lens": lens_id,
        "band": verdict.get("band"),
        "label": verdict.get("label"),
        "means": verdict.get("means"),
        "as_of": exh.as_of if exh else None,
        "freshness": exh.freshness if exh else "missing",
        "lamp": lamp_for(exh, selected_date),
        "to": hub_route(lens_id),
        "readings": dict(exh.readings) if exh else {},
        "track_record": exh.track_record if exh else None,
        "similar": exh.similar if exh else None,
        "caveats": list(exh.caveats) if exh else [],
    }
    if detail is not None or (exh is not None and has_reading(exh)):
        # Legacy `detail`: the readings with the as-of date, for readers of the pre-C3 shape.
        card["detail"] = detail if detail is not None else {**exh.readings, "trade_date": exh.as_of}  # type: ignore[union-attr]
    else:
        card["detail"] = None
    card.update(extra)
    return card
