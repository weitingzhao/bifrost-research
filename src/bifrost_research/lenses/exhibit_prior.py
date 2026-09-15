"""The same lens, one session earlier — ``ExhibitResponse.prior``.

A reading with no yesterday is a number without a direction: IV Rank 62 reads
differently when yesterday was 41 than when it was 78. This module answers
"what did this lens say on the previous session" with the lens' own reading and
the *same* registry band function the current verdict uses — no thresholds are
written here, and none in SQL.

Three shapes:

- **row priors** — one row per ``trade_date`` and the banded reading is a plain
  column (IV Rank, IV Percentile, VRP, Momentum, SEPA, terrain regime): the
  previous row on the lens' own table, taken from the registry's
  ``source_table`` / ``value_column``.
- **built priors** — the banded reading is computed (skew's own-year percentile,
  near − far term slope, pin distance against the close, gamma sign): the
  reader's own helpers are run against the previous date, so the prior can never
  drift from the current reading.
- **no prior** — ``forecast_path`` is a 30-session aggregate, not a daily row.

Failures degrade to ``prior = None`` plus a caveat; an exhibit without a prior
is still an exhibit.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from bifrost_research.lenses.exhibit_lenses import (
    TAPE_SOURCE,
    VERDICT_INPUT,
    backwardation_from,
    close_on,
    gex_level_row,
    gex_regime_label,
    max_pain_row,
    near_far_fits,
    pin_distance,
    skew_fit_row,
    skew_slope_pctile,
    term_fit_rows,
)
from bifrost_research.lenses.exhibit_model import ExhibitResponse, iso_date, rollback_quietly
from bifrost_research.lenses.registry import LENSES, classify, classify_category

logger = logging.getLogger(__name__)

# One row per trade_date, banded reading is a column on the registry's source table.
ROW_PRIOR_LENSES = ("iv_rank", "iv_percentile", "vrp", "momentum", "sepa", "terrain_regime")
# Aggregate readings have no previous row to point at.
NO_PRIOR_LENSES = ("forecast_path",)


def _banded(lens_id: str, trade_date: Any, value: Any) -> dict[str, Any]:
    """``{as_of, value, band}`` — band from the registry, exactly as the verdict gets it."""
    spec = LENSES[lens_id]
    if spec.kind == "categorical":
        band = classify_category(lens_id, None if value is None else str(value))
    else:
        band = classify(lens_id, value, fractions_as_pct=VERDICT_INPUT[lens_id][1])
    return {"as_of": iso_date(trade_date), "value": value, "band": band}


def _row_prior(
    conn: Any,
    lens_id: str,
    symbol: str,
    before: str,
    *,
    extra: str | None = None,
) -> tuple[Any, ...] | None:
    spec = LENSES[lens_id]
    columns = spec.value_column if extra is None else f"{spec.value_column}, {extra}"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trade_date, {columns}
            FROM {spec.source_table}
            WHERE symbol = %s AND trade_date < %s::date AND {spec.value_column} IS NOT NULL
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (symbol, before),
        )
        return cur.fetchone()


def _prior_skew(conn: Any, symbol: str, before: str) -> dict[str, Any] | None:
    row = skew_fit_row(conn, symbol, before)
    if not row:
        return None
    abs_slope = abs(float(row[4])) if row[4] is not None else None
    hist = skew_slope_pctile(conn, symbol, row[0], abs_slope)
    pctile = float(hist[2]) if hist and hist[2] is not None else None
    return _banded("skew", row[0], pctile)


def _prior_term_slope(conn: Any, symbol: str, before: str) -> dict[str, Any] | None:
    rows = term_fit_rows(conn, symbol, before)
    if not rows:
        return None
    near, far = near_far_fits(rows)
    return _banded("term_slope", near[0], backwardation_from(near, far))


def _prior_gex_regime(conn: Any, symbol: str, before: str) -> dict[str, Any] | None:
    row = gex_level_row(conn, symbol, before)
    if not row:
        return None
    return _banded("gex_regime", row[0], gex_regime_label(row[3]))


def _prior_opex_pin(conn: Any, symbol: str, before: str) -> dict[str, Any] | None:
    row = max_pain_row(conn, symbol, before)
    if not row:
        return None
    return _banded("opex_pin", row[0], pin_distance(close_on(conn, symbol, row[0]), row[2]))


def _prior_order_sentiment(conn: Any, symbol: str, before: str) -> dict[str, Any] | None:
    row = _row_prior(conn, "order_sentiment", symbol, before, extra="data_source")
    if not row:
        return None
    if row[2] != TAPE_SOURCE:
        # Same rule as the exhibit: without the trades tape the score is an OI
        # proxy, so it carries a value but no band.
        return {"as_of": iso_date(row[0]), "value": row[1], "band": None}
    return _banded("order_sentiment", row[0], row[1])


_BUILT_PRIORS: dict[str, Callable[[Any, str, str], dict[str, Any] | None]] = {
    "skew": _prior_skew,
    "term_slope": _prior_term_slope,
    "gex_regime": _prior_gex_regime,
    "opex_pin": _prior_opex_pin,
    "order_sentiment": _prior_order_sentiment,
}


def prior_reading(conn: Any, lens_id: str, symbol: str, as_of: str | None) -> dict[str, Any] | None:
    """The lens' reading on the last session before ``as_of``, or None."""
    if as_of is None or lens_id in NO_PRIOR_LENSES:
        return None
    built = _BUILT_PRIORS.get(lens_id)
    if built is not None:
        return built(conn, symbol, as_of)
    if lens_id in ROW_PRIOR_LENSES:
        row = _row_prior(conn, lens_id, symbol, as_of)
        return _banded(lens_id, row[0], row[1]) if row else None
    return None


def attach_prior(conn: Any, exh: ExhibitResponse, lens_id: str) -> ExhibitResponse:
    """Set ``exh.prior``; a failed prior is a caveat, never a failed exhibit."""
    try:
        exh.prior = prior_reading(conn, lens_id, exh.symbol, exh.as_of)
    except Exception as exc:  # noqa: BLE001 — the previous session must not sink this one
        logger.debug("prior failed for %s: %s", lens_id, exc)
        rollback_quietly(conn)
        exh.prior = None
        exh.caveats.append("Previous-session reading unavailable")
    return exh
