"""Earnings dates from a name's own 8-Ks — one definition for every reader.

An 8-K carrying Item 2.02 (results of operations) is the results release, nearly
always. Some companies furnish other numbers under the same item: Tesla files
each quarter's production and deliveries there, about three weeks ahead of its
results. Such a filing says nothing about results once the item headings are set
aside, and the name's results release follows within weeks; a filing that is
both is not a print.

Measured over the feed on 2026-09-25 (4,971 filing days carrying Item 2.02, 611
names): 28 say nothing about results. Eleven are followed by a release within
``RELEASE_WITHIN_DAYS`` and are set aside — Tesla's eight delivery reports, First
Solar's tax-credit sale (2025-02-20, five days before its results), IREN's
business update (2026-07-20) and EGY's asset purchase (2026-02-10). The other 17
are kept: each is the name's only Item 2.02 that quarter, the release's words all
in the exhibit (AMKR, MELI, GGG, AZO 2025-03-04).

The newest such filing has no release after it until the release comes, so every
quarter Tesla's deliveries would stand as the latest print for three weeks. A
name with ``HABIT_MIN`` filings already set aside has shown the habit, and its
trailing ones are set aside too.

Read-only: ``raw_market.sec_8k_filing``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Sequence

# Every 2.02 filing carries its item headings; they name "results" whatever the
# filing says. Tesla's 2024-10-02 filing spells its heading "Results from Operations".
_HEADINGS = re.compile(
    r"results (?:of|from) operations? and financial conditions?"
    r"|financial statements and exhibits"
    r"|regulation fd disclosure"
    r"|other events",
    re.IGNORECASE,
)
_RESULTS_WORDS = re.compile(
    r"\b(?:results|earnings|quarter|quarterly|fiscal|revenues?|net income|per share|guidance|outlook)\b",
    re.IGNORECASE,
)

RELEASE_WITHIN_DAYS = 45
HABIT_MIN = 2


def speaks_of_results(text: str | None) -> bool:
    """Whether the filing's own words, headings aside, are about results. A filing
    with no text on file cannot be judged and counts as a release."""
    if not text or not text.strip():
        return True
    return bool(_RESULTS_WORDS.search(_HEADINGS.sub(" ", " ".join(text.split()))))


def split_releases(filings: Sequence[tuple[date, bool]]) -> tuple[list[date], list[dict[str, Any]]]:
    """Split a name's Item 2.02 filings into print dates (oldest first) and the ones
    set aside. ``filings`` is (filing_date, speaks_of_results) per filing; a day
    speaks of results if any of its filings does."""
    by_day: dict[date, bool] = {}
    for d, worded in filings:
        by_day[d] = by_day.get(d, False) or worded
    releases = sorted(d for d, w in by_day.items() if w)

    kept: list[date] = []
    aside: list[dict[str, Any]] = []
    for d in sorted(by_day):
        if by_day[d]:
            kept.append(d)
            continue
        release = next((r for r in releases if r > d), None)
        if release is not None and (release - d).days <= RELEASE_WITHIN_DAYS:
            aside.append(
                {
                    "filed": d.isoformat(),
                    "release": release.isoformat(),
                    "reason": f"says nothing about results; the results release followed on {release.isoformat()}",
                }
            )
        else:
            kept.append(d)

    if len(aside) >= HABIT_MIN:
        last_release = releases[-1] if releases else date.min
        trailing = [d for d in kept if not by_day[d] and d > last_release]
        for d in trailing:
            kept.remove(d)
            aside.append(
                {
                    "filed": d.isoformat(),
                    "release": None,
                    "reason": (
                        f"says nothing about results, like the {len(aside)} earlier filings of this name set aside; "
                        "no results release has followed yet"
                    ),
                }
            )
    return kept, aside


def fetch_item_202(conn: Any, symbol: str, *, as_of: date | None = None) -> list[tuple[date, bool]]:
    """The name's Item 2.02 filings as (filing_date, speaks_of_results), on or before ``as_of``."""
    sql = """
        SELECT filing_date, items_text
        FROM raw_market.sec_8k_filing
        WHERE symbol = %s AND '2.02' = ANY(items)
    """
    params: tuple[Any, ...] = (symbol,)
    if as_of is not None:
        sql += " AND filing_date <= %s"
        params = (symbol, as_of)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() or []
    return [(d, speaks_of_results(t)) for d, t in rows]


__all__ = [
    "HABIT_MIN",
    "RELEASE_WITHIN_DAYS",
    "split_releases",
    "fetch_item_202",
    "speaks_of_results",
]
