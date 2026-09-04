"""The template's market-wide baseline, computed once a day.

Two problems with calling `run_event_query` inline from `run_objective`.

It costs ~29 seconds — measured twice back to back at 28.8s and 29.1s — and
returns the identical result each time, because the event definition carries no
parameters and the query never sees the run's candidates. Every run that plans a
backtest paid a half minute to recompute an answer that changes at most once a
day, when new earnings settle.

And the answer is about the *template*, not about the symbols. `n_events=37,
win_rate=0.3243` is the record of `long_stock_event` across the market over three
years; attached to a batch without saying so, a reader takes it for evidence
about the eight names in front of them. The loop_curator has already flagged
exactly this in prose — "identical across every symbol … I did not use them to
justify bullish execution verdicts" — so the payload now says what the number is
before anyone has to work it out again.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

# (event kind, template, lookback, day) → summary. Day-scoped because settled
# earnings history is what moves the answer, and that moves overnight.
_cache: dict[tuple[str, str, int, str], dict[str, Any]] = {}


def clear_baseline_cache() -> None:
    """For tests, and for a caller that knows the warehouse just changed."""
    _cache.clear()


def template_baseline(
    conn: Any,
    *,
    event_kind: str = "earnings",
    template: str = "long_stock_event",
    lookback_years: int = 3,
    today: date | None = None,
) -> dict[str, Any]:
    """Market-wide record for a template. Never raises — a failed baseline must
    not cost the Owner the batch, so it comes back as ``not_measured``."""
    day = (today or date.today()).isoformat()
    key = (event_kind, template, lookback_years, day)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    try:
        from bifrost_research.engines.backtest.event_defs import EventDef
        from bifrost_research.engines.backtest.event_query import run_event_query

        bt = run_event_query(
            EventDef(kind=event_kind, params={}),
            template,
            lookback_years=lookback_years,
            conn=conn,
        )
        summary: dict[str, Any] = {
            "status": "ok",
            "template": template,
            # The reader's guard rail. Without it a per-symbol reading of a
            # market-wide aggregate looks like symbol evidence.
            "scope": "market_wide",
            "scope_note": (
                f"{template} across the whole market over {lookback_years}y — "
                "not a record for the candidates in this batch"
            ),
            "lookback_years": lookback_years,
            "as_of": day,
            "summary": bt.get("summary") or {},
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("template baseline failed (%s/%s): %s", event_kind, template, exc)
        # Not cached: a transient warehouse failure must not pin "not measured"
        # for the rest of the day.
        return {"status": "not_measured", "reason": str(exc)[:200], "scope": "market_wide"}

    _cache[key] = summary
    return summary


__all__ = ["template_baseline", "clear_baseline_cache"]
