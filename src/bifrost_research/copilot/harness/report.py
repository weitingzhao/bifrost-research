"""Turn a proposed batch into something that can be judged — P1 compose_report.

The Loop's deliverable was a list of symbols with scores. A score with no
reasoning is an opinion, so every recommendation here carries four sections:

  why        — the signal and where it sits in its own distribution
  price      — where the price is relative to its own structure
  settled    — how this source has actually performed (research.candidate_outcome)
  wrong_if   — what would make the call wrong

The third is the one that matters and the one most easily faked. It comes from
the outcome ledger, and when nothing has settled yet it says so: "not measured"
is a fact about our coverage, not a verdict on the stock, and rendering it as a
zero would be a lie the reader cannot see through.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _section_for(item: dict[str, Any]) -> dict[str, Any]:
    ev = item.get("evidence") or {}
    sel = ev.get("selection") or {}
    price = ev.get("price_context") or {}
    rec = ev.get("track_record") or {}

    horizons = [h for h in (rec.get("horizons") or []) if h.get("hit_rate") is not None]
    if horizons:
        settled: dict[str, Any] = {
            "status": "measured",
            "horizons": [
                {"horizon_days": h.get("horizon_days"), "hit_rate": h.get("hit_rate")}
                for h in horizons
            ],
        }
    else:
        settled = {
            "status": "not_measured",
            "reason": rec.get("reason")
            or "no candidate from this source has reached a settled horizon yet",
        }

    return {
        "symbol": item.get("symbol"),
        "score": item.get("score"),
        "why": {
            "path": sel.get("path"),
            "grade": sel.get("grade"),
            "stage": sel.get("stage"),
            "sepa_score": sel.get("sepa_score"),
            "components": sel.get("components"),
        },
        "price": {
            "close": price.get("close"),
            "sma_50": price.get("sma_50"),
            "sma_200": price.get("sma_200"),
            "pct_off_52w_high": price.get("pct_off_52w_high"),
        },
        "settled": settled,
        "wrong_if": ev.get("invalidation") or [],
    }


def compose_report(
    *,
    objective: dict[str, Any],
    run_id: str,
    items: list[dict[str, Any]],
    funnel: list[dict[str, Any]] | None,
    backtest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the batch report. Pure — no DB, no network, so it cannot fail a run."""
    sections = [_section_for(i) for i in items]
    measured = sum(1 for s in sections if s["settled"]["status"] == "measured")
    return {
        "objective_id": objective.get("id"),
        "objective_title": objective.get("title"),
        "run_id": run_id,
        "candidates": sections,
        # The funnel is what makes this a screen rather than a watchlist reread.
        "funnel": funnel or [],
        "backtest": backtest,
        "coverage": {
            "candidates": len(sections),
            "with_settled_record": measured,
            # Stated rather than implied: a reader should know how much of this
            # report rests on measured history and how much on selection alone.
            "note": (
                f"{measured} of {len(sections)} candidates have a settled record; "
                "the rest are proposals whose source has not been scored yet"
            )
            if sections
            else "no candidates in this batch",
        },
        "advisory": "D10 BLOCKED — research proposal only, never an order",
    }
