"""US monthly OpEx calendar — Wave RS-B-OpEx1.

Standard US equity/index monthly options expire on the **third Friday** of
each month. Weekly / EOM / quarterly cycles are out of scope for v1; use
`third_friday`/`next_opex_friday` for the canonical monthly OpEx anchor.

Pure Python, no external dependencies (no `pandas.tseries.offsets`).
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta


def third_friday(year: int, month: int) -> date:
    """Return the third Friday of ``(year, month)``.

    Algorithm: find the first Friday (weekday==4 in ISO Monday=0 sense),
    then add 14 days.
    """
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1..12, got {month}")
    _, days_in_month = calendar.monthrange(year, month)
    for day in range(1, min(8, days_in_month + 1)):
        d = date(year, month, day)
        if d.weekday() == calendar.FRIDAY:
            first_friday = d
            break
    else:  # pragma: no cover — unreachable
        raise RuntimeError("no Friday found in first 7 days")
    third = first_friday + timedelta(days=14)
    if third.month != month:  # pragma: no cover — should never happen
        raise RuntimeError(f"third Friday spilled into next month: {third}")
    return third


def next_opex_friday(today: date) -> date:
    """Next US monthly OpEx (third Friday) on or **after** ``today``.

    If ``today`` is itself the third Friday, roll forward to next month.
    """
    this_month = third_friday(today.year, today.month)
    if today < this_month:
        return this_month
    # Roll to next month
    next_month = today.month + 1
    next_year = today.year
    if next_month > 12:
        next_month = 1
        next_year += 1
    return third_friday(next_year, next_month)


def days_to_opex(today: date) -> int:
    """Calendar days until the next monthly OpEx Friday.

    Returns 0 when ``today`` is the OpEx Friday (before the roll).
    """
    this_month = third_friday(today.year, today.month)
    if today <= this_month:
        return (this_month - today).days
    return (next_opex_friday(today) - today).days


def is_opex_week(today: date) -> bool:
    """True when ``today`` falls in the Monday–Friday of a monthly OpEx week.

    Definition: same ISO year+week as the third Friday of ``today``'s month.
    """
    this_month = third_friday(today.year, today.month)
    iso_today = today.isocalendar()
    iso_friday = this_month.isocalendar()
    return iso_today.year == iso_friday.year and iso_today.week == iso_friday.week
