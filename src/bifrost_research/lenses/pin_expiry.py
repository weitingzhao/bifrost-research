"""The expiry the OpEx pin lens reads: the next monthly (Owner 2026-09-26).

Until then every pin reader took the expiry nearest 30 days out, which is a
weekly for most of the month (PLTR 2026-09-25: 10-23, 28 DTE) while the pin
record — ``opex_cycle.get_pin_analysis`` — is measured on the monthly cycles.
The weekly held about a twelfth of the monthly's open interest (PLTR 27,497
against 311,799; SPY 153,044 against 2,514,711), so the reading and its record
were about different expiries, and the reading about the thin one.

A monthly expiry is the third Friday, or the Thursday before it when that
Friday is a market holiday (2026-06-19, Juneteenth). The next one is the
earliest strictly after the session: on OpEx day itself the cycle has closed.

Weekly names carry their first three expiries in the collected chain, so for
about two weeks after each OpEx the next monthly is not there yet (2026-09-18:
289 of 610 names). Those names have no pin reading until it arrives; the pin
readers do not fall back to a weekly.
"""

from __future__ import annotations


def monthly_expiry_sql(expiry: str = "expiry", trade_date: str = "trade_date") -> str:
    """SQL predicate: ``expiry`` is a monthly expiry after ``trade_date``."""
    return f"""(
        {expiry} > {trade_date}
        AND (
            (extract(dow from {expiry}) = 5 AND extract(day from {expiry}) BETWEEN 15 AND 21)
            OR (
                extract(dow from {expiry}) = 4
                AND extract(day from {expiry} + 1) BETWEEN 15 AND 21
                AND EXISTS (
                    SELECT 1 FROM raw_market.us_market_holiday h
                    WHERE h.holiday_date = {expiry} + 1 AND h.status = 'closed'
                )
            )
        )
    )"""


NO_MONTHLY_CAVEAT = (
    "The next monthly expiry is not in the collected chain yet — weekly names carry "
    "their first three expiries, so it arrives about two weeks before OpEx"
)
