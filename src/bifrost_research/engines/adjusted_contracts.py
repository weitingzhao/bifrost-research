"""Adjusted option contracts stay out of every reader of raw option data.

An adjusted contract (``O:APTV1…``, ``O:HON2…``) is what a corporate action leaves
behind: the OCC root is the underlying's plus a digit and the deliverable is no
longer 100 shares of the underlying, yet the vendor still solves its IV and greeks
against the underlying's close. On 2026-09-29 the APTV1 35 call read 346% beside
52% for the standard APTV 35 call. The root is what the ticker holds between
``O:`` and its last 15 characters (YYMMDD, right, strike); other root aliases
(SPXW for SPX, BRKB for BRK.B) do not end in a digit.

Measured 2026-10-01 (read-only, before this filter reached them): the contracts sat
in 2,385 reconstructed-IV rows, moved ATM IV on 249 name-sessions back to 2024-10
(``option_daily`` keeps expired adjusted contracts that ``option_contract`` no longer
lists), GEX levels on 205, flow sentiment on 218 and PCR on 233.
"""

from __future__ import annotations

from typing import Any


def not_adjusted_contract_sql(column: str) -> str:
    """SQL predicate: the option ticker in ``column`` is not an adjusted contract."""
    return f"substr({column}, 3, length({column}) - 17) !~ '[0-9]$'"


def option_listing(conn: Any, symbol: str) -> dict[str, Any] | None:
    """How many standard and adjusted contracts the name listed on its latest
    open-interest session (TD-159) — by the same predicate every reader filters
    on, so a name with only adjusted contracts (CUE after its 1:30 reverse split:
    14 ``CUE1`` contracts, 0 standard on 2026-10-05) is said to be one rather than
    inferred from ticker shapes in the browser. ``None`` when the name has no
    open-interest rows at all. Read-only: ``raw_market.option_open_interest``."""
    standard = not_adjusted_contract_sql("option_ticker")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trade_date,
                   COUNT(*) FILTER (WHERE {standard}),
                   COUNT(*) FILTER (WHERE NOT ({standard})),
                   COALESCE(
                       array_agg(DISTINCT substr(option_ticker, 3, length(option_ticker) - 17))
                           FILTER (WHERE NOT ({standard})),
                       '{{}}'
                   )
            FROM raw_market.option_open_interest
            WHERE underlying = %s
              AND trade_date = (
                  SELECT MAX(trade_date) FROM raw_market.option_open_interest WHERE underlying = %s
              )
            GROUP BY trade_date
            """,
            (symbol, symbol),
        )
        row = cur.fetchone()
    if not row:
        return None
    as_of, n_standard, n_adjusted, roots = row
    return {
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else as_of,
        "standard_contracts": int(n_standard or 0),
        "adjusted_contracts": int(n_adjusted or 0),
        "adjusted_roots": sorted(roots or []),
    }


__all__ = ["not_adjusted_contract_sql", "option_listing"]
