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


def not_adjusted_contract_sql(column: str) -> str:
    """SQL predicate: the option ticker in ``column`` is not an adjusted contract."""
    return f"substr({column}, 3, length({column}) - 17) !~ '[0-9]$'"


__all__ = ["not_adjusted_contract_sql"]
