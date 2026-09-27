"""Whether a listing still trades as of a session: one definition for every reader.

A name counts as retired only when two signals agree. Its ``raw_market.ticker``
row must say inactive, and ``raw_market.stock_daily`` must have no close for it
within ``LIVENESS_DAYS``. The Plugin's snapshot skip (0.48.0) and
``engines/retired_listing_purge`` ask about the same pair. Either signal alone
misreads a name. The flag lags the vendor's retirement by weeks: AVB was
delisted 2026-08-18 and flagged inactive 2026-09-06. And a halt or a holiday
week stops the closes of a live name.
"""

from __future__ import annotations

from datetime import date, timedelta

from bifrost_research.engines.option_universe.entry import LIVENESS_DAYS


def retired_sql(symbol_ref: str) -> str:
    """An ``EXISTS`` that is true when ``symbol_ref`` names a retired listing.

    Binds one parameter, the date from :func:`liveness_floor`.
    ``symbol_ref`` is a column reference from the caller's own query, not input.
    """
    return f"""EXISTS (
                SELECT 1 FROM raw_market.ticker AS t
                WHERE t.symbol = {symbol_ref}
                  AND t.active IS FALSE
                  AND NOT EXISTS (
                      SELECT 1 FROM raw_market.stock_daily AS s
                      WHERE s.symbol = {symbol_ref} AND s.bar_date > %s
                  )
            )"""


def liveness_floor(as_of: date) -> date:
    """A close dated after this means the listing still trades as of ``as_of``."""
    return as_of - timedelta(days=LIVENESS_DAYS)


def split_retired(left_out: list[dict]) -> tuple[list[dict], int]:
    """Left-out names without the retired ones, and how many were retired.

    A retired listing is not a gap anyone can close, and nothing bounds how far
    back the left-out read looks. Listed, the names would stay on the list for
    good. So they are counted rather than named.
    """
    kept = [d for d in left_out if d.get("reason") != "retired"]
    return kept, len(left_out) - len(kept)
