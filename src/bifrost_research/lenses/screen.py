"""Bulk screening — one universe, many lenses, one query per lens.

The foundation could read one symbol deeply (``build_exhibit``) but could not
ask a question of every symbol at once, so every caller that needed breadth
wrote its own SQL over its own tables: the harness screens on three of the ten
lens tables and never sees the option faces at all (contract C-A1), and the
pages read one symbol at a time. This module is the missing primitive (C-F1).

Two properties make it worth having rather than looping ``build_exhibit``:

* **Set-based.** One statement per lens over the whole universe — 575 symbols
  cost twelve queries, not 6,900. A loop over ``build_exhibit`` at ~1.2s each
  would take eleven minutes per screen.
* **Same definitions.** Bands come from ``lenses.registry`` — the same
  ``classify`` the exhibit, the verdict strip and Signal Decay use. A screen
  can never disagree with the page it links to.

And one property the seam contract needs (C-A2): every row says which lenses
had **no reading for that symbol**, so a caller can refuse to send a name into
judgement on faces it does not have, instead of silently judging on fewer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from bifrost_research.lenses.registry import LENSES, classify, classify_category

logger = logging.getLogger(__name__)

# Long enough to survive a holiday weekend (the tables lag each other by a day
# or two), short enough that a stalled engine reads as missing rather than as a
# stale reading presented as today's.
DEFAULT_WINDOW_DAYS = 7
# Screens run over the whole universe; the role's default is far too tight.
STATEMENT_TIMEOUT = "60s"
UNIVERSE_TABLE = "research.option_universe"


@dataclass(frozen=True)
class LensReading:
    lens: str
    value: Any
    band: str | None
    as_of: date | None


@dataclass(frozen=True)
class ScreenRow:
    symbol: str
    readings: dict[str, LensReading]
    """Lenses that were asked for and have no reading for this symbol — the
    faces this name does not have. C-A2: judgement must not pretend otherwise."""
    missing: tuple[str, ...]
    survived: bool


@dataclass(frozen=True)
class ScreenResult:
    lenses: tuple[str, ...]
    universe: tuple[str, ...]
    rows: tuple[ScreenRow, ...]
    """Lens id → how many symbols in the universe had a reading."""
    coverage: dict[str, int] = field(default_factory=dict)
    """Lens id → why it could not be screened. Never silently dropped."""
    unscreenable: dict[str, str] = field(default_factory=dict)

    @property
    def survivors(self) -> tuple[ScreenRow, ...]:
        return tuple(r for r in self.rows if r.survived)


# --------------------------------------------------------------------------
# Per-lens readers
#
# A reader returns SQL producing (symbol, value, as_of), one row per symbol.
# Most lenses are the registry's own column on the registry's own table; the
# four whose `value_column` is a name the exhibit computes rather than a stored
# column need their own statement, and two of those are not worth screening at
# all today — they say so rather than answering with a number they invented.
# --------------------------------------------------------------------------

Reader = Callable[[Any, Sequence[str], date, int], str]

UNSCREENABLE: dict[str, str] = {
    # The exhibit ranks atm_slope against its own 252-day history; the table
    # starts 2026-08-21, so every percentile would be drawn from days, not a
    # year. The lens is behind a 60-session maturity gate for the same reason.
    "skew": "needs a 252-day percentile of atm_slope; the table has weeks of history",
    # near/far vol by dte, two rows per symbol from the surface fit; worth
    # adding when the surface covers more than the resident names.
    "term_slope": "derived from two dte rows per symbol; no set-based reader yet",
}


def _latest_column(table: str, column: str) -> str:
    """The newest row per symbol in the window — the default reader."""
    return f"""
        SELECT DISTINCT ON (symbol) symbol, {column} AS value, trade_date AS as_of
        FROM {table}
        WHERE symbol = ANY(%(symbols)s) AND trade_date >= %(since)s
        ORDER BY symbol, trade_date DESC
    """


def _gex_regime_sql() -> str:
    """The registry bands the *sign* of net gamma, not the number itself."""
    return """
        SELECT DISTINCT ON (symbol)
               symbol,
               CASE WHEN total_net_gex < 0 THEN 'negative' ELSE 'positive' END AS value,
               trade_date AS as_of
        FROM features.option_metric_gex_levels_daily
        WHERE symbol = ANY(%(symbols)s) AND trade_date >= %(since)s
          AND total_net_gex IS NOT NULL
        ORDER BY symbol, trade_date DESC
    """


def _order_sentiment_sql() -> str:
    """Only the trades tape carries a verdict; the OI proxy is not evidence.

    Same gate as the exhibit (`exhibit_lenses.py`), so a screen and a page
    agree about which symbols have this face at all.
    """
    return """
        SELECT DISTINCT ON (symbol) symbol, sentiment_score AS value, trade_date AS as_of
        FROM features.option_flow_sentiment_daily
        WHERE symbol = ANY(%(symbols)s) AND trade_date >= %(since)s
          AND data_source = 'option_trades_tape'
        ORDER BY symbol, trade_date DESC
    """


def _opex_pin_sql() -> str:
    """Pin distance = (close − max pain) / close, on the newest max-pain date.

    The expiry nearest 30 days out is the one the exhibit reads, so the screen
    reads the same one.
    """
    return """
        WITH latest AS (
            SELECT DISTINCT ON (symbol) symbol, trade_date, expiry, max_pain_strike
            FROM features.option_metric_max_pain_daily
            WHERE symbol = ANY(%(symbols)s) AND trade_date >= %(since)s
              AND max_pain_strike IS NOT NULL
            ORDER BY symbol, trade_date DESC, ABS((expiry - trade_date) - 30) ASC, expiry ASC
        )
        SELECT l.symbol,
               (s.close - l.max_pain_strike) / NULLIF(s.close, 0) AS value,
               l.trade_date AS as_of
        FROM latest l
        JOIN raw_market.stock_daily s
          ON s.symbol = l.symbol AND s.bar_date = l.trade_date
        WHERE s.close IS NOT NULL AND s.close > 0
    """


def _forecast_path_sql() -> str:
    """The lens reads a hit *rate* over the window, not one settlement."""
    return """
        SELECT symbol,
               CASE WHEN avg((path_hit)::int) >= 0.5 THEN 'hit' ELSE 'miss' END AS value,
               max(trade_date) AS as_of
        FROM features.stock_backtest_settlement
        WHERE symbol = ANY(%(symbols)s) AND trade_date >= %(since)s
          AND path_hit IS NOT NULL
        GROUP BY symbol
    """


_SQL_OVERRIDES: dict[str, Callable[[], str]] = {
    "gex_regime": _gex_regime_sql,
    "order_sentiment": _order_sentiment_sql,
    "opex_pin": _opex_pin_sql,
    "forecast_path": _forecast_path_sql,
}


def lens_sql(lens_id: str) -> str | None:
    """The statement that reads one lens for many symbols, or None if it has none."""
    if lens_id in UNSCREENABLE:
        return None
    override = _SQL_OVERRIDES.get(lens_id)
    if override is not None:
        return override()
    spec = LENSES[lens_id]
    if not spec.value_column:
        return None
    return _latest_column(spec.source_table, spec.value_column)


def band_for(lens_id: str, value: Any) -> str | None:
    """The registry's band for a reading — the same call the exhibit makes."""
    spec = LENSES[lens_id]
    if spec.kind == "categorical":
        return classify_category(lens_id, None if value is None else str(value))
    return classify(lens_id, value)


# --------------------------------------------------------------------------
# Universe
# --------------------------------------------------------------------------


def load_universe(conn: Any, *, tiers: Sequence[str] | None = None) -> tuple[str, ...]:
    """The option universe, by rule — the same table the Plugin collects against."""
    sql = f"SELECT symbol FROM {UNIVERSE_TABLE}"
    params: list[Any] = []
    if tiers:
        sql += " WHERE tier = ANY(%s)"
        params.append(list(tiers))
    sql += " ORDER BY symbol"
    with conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        cur.execute(sql, params or None)
        return tuple(r[0] for r in cur.fetchall() or [])


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


def _read_lens(
    conn: Any,
    lens_id: str,
    symbols: Sequence[str],
    since: date,
) -> dict[str, LensReading]:
    """One statement, every symbol. Raises rather than answering partially."""
    sql = lens_sql(lens_id)
    if sql is None:
        return {}
    with conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        cur.execute(sql, {"symbols": list(symbols), "since": since})
        rows = cur.fetchall() or []
    out: dict[str, LensReading] = {}
    for symbol, value, as_of in rows:
        out[symbol] = LensReading(
            lens=lens_id, value=value, band=band_for(lens_id, value), as_of=as_of
        )
    return out


def screen(
    conn: Any,
    *,
    lenses: Iterable[str],
    symbols: Sequence[str] | None = None,
    tiers: Sequence[str] | None = None,
    require: Mapping[str, Iterable[str]] | None = None,
    as_of: date | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> ScreenResult:
    """Read every lens for every symbol, band them, and say who survives.

    ``require`` is ``{lens_id: bands}``: a symbol survives when each named lens
    has a reading in one of those bands. A symbol missing a required lens does
    not survive — a face you cannot see is not a face that passed.
    """
    wanted = tuple(dict.fromkeys(lenses))
    unknown = [x for x in wanted if x not in LENSES]
    if unknown:
        raise ValueError(f"unknown lens: {', '.join(unknown)}")
    universe = tuple(symbols) if symbols is not None else load_universe(conn, tiers=tiers)
    since = (as_of or date.today()) - timedelta(days=window_days)

    unscreenable = {lens: UNSCREENABLE[lens] for lens in wanted if lens in UNSCREENABLE}
    readable = [lens for lens in wanted if lens not in unscreenable]

    by_lens: dict[str, dict[str, LensReading]] = {}
    if universe:
        for lens_id in readable:
            by_lens[lens_id] = _read_lens(conn, lens_id, universe, since)

    required = {k: set(v) for k, v in (require or {}).items()}
    rows: list[ScreenRow] = []
    for symbol in universe:
        readings = {lens: r[symbol] for lens, r in by_lens.items() if symbol in r}
        missing = tuple(lens for lens in readable if lens not in readings)
        survived = all(
            (lens in readings) and (readings[lens].band in bands)
            for lens, bands in required.items()
        )
        rows.append(ScreenRow(symbol=symbol, readings=readings, missing=missing, survived=survived))
    return ScreenResult(
        lenses=wanted,
        universe=universe,
        rows=tuple(rows),
        coverage={lens: len(found) for lens, found in by_lens.items()},
        unscreenable=unscreenable,
    )
