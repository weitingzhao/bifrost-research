"""Which Item 2.02 filings are results releases. Texts and dates are invented,
shaped like the feed's (a delivery report, a release, an exhibit-only release)."""

from __future__ import annotations

from datetime import date

from bifrost_research.repositories.earnings_filings import (
    HABIT_MIN,
    RELEASE_WITHIN_DAYS,
    speaks_of_results,
    split_releases,
)

DELIVERIES = (
    "Item 2.02 Results of Operations and Financial Condition. On April 2, 2031, Zeta Motors, Inc. "
    "published the press release which is attached hereto as Exhibit 99.1 and is incorporated herein "
    "by reference. This information is intended to be furnished under Item 2.02 of Form 8-K, "
    '"Results of Operations and Financial Condition." Item 9.01 Financial Statements and Exhibits.'
)
RELEASE = (
    "Item 2.02 Results of Operations and Financial Condition. On April 22, 2031, Zeta Motors, Inc. "
    "released its financial results for the quarter ended March 31, 2031."
)


def test_the_headings_do_not_count_as_talk_of_results() -> None:
    assert speaks_of_results(DELIVERIES) is False
    assert speaks_of_results(DELIVERIES.replace("Results of Operations", "Results from Operations", 1)) is False
    assert speaks_of_results(RELEASE) is True


def test_a_filing_with_no_text_cannot_be_judged_and_stays_a_release() -> None:
    assert speaks_of_results(None) is True
    assert speaks_of_results("   ") is True


def test_a_quiet_filing_before_a_release_is_set_aside() -> None:
    kept, aside = split_releases([(date(2031, 4, 2), False), (date(2031, 4, 22), True)])
    assert kept == [date(2031, 4, 22)]
    assert aside == [
        {
            "filed": "2031-04-02",
            "release": "2031-04-22",
            "reason": "says nothing about results; the results release followed on 2031-04-22",
        }
    ]


def test_five_days_ahead_is_still_not_the_print() -> None:
    kept, aside = split_releases([(date(2031, 2, 20), False), (date(2031, 2, 25), True)])
    assert kept == [date(2031, 2, 25)] and aside[0]["filed"] == "2031-02-20"


def test_a_quiet_filing_that_is_the_quarters_only_one_is_the_print() -> None:
    # The release's words all in the exhibit: nothing follows it within the window.
    far = date(2031, 4, 2).toordinal() + RELEASE_WITHIN_DAYS + 1
    kept, aside = split_releases([(date(2031, 4, 2), False), (date.fromordinal(far), True)])
    assert kept == [date(2031, 4, 2), date.fromordinal(far)] and aside == []


def test_a_day_with_any_release_is_a_release() -> None:
    kept, aside = split_releases([(date(2031, 4, 22), False), (date(2031, 4, 22), True)])
    assert kept == [date(2031, 4, 22)] and aside == []


def test_a_name_with_the_habit_has_its_trailing_quiet_filing_set_aside() -> None:
    quarters = [(date(2031, 1, 2), False), (date(2031, 1, 28), True), (date(2031, 4, 2), False), (date(2031, 4, 22), True)]
    assert HABIT_MIN == 2
    kept, aside = split_releases([*quarters, (date(2031, 7, 2), False)])
    assert kept == [date(2031, 1, 28), date(2031, 4, 22)]
    assert aside[-1]["filed"] == "2031-07-02" and aside[-1]["release"] is None
    assert "like the 2 earlier filings" in aside[-1]["reason"]


def test_one_set_aside_is_not_a_habit() -> None:
    kept, _aside = split_releases([(date(2031, 4, 2), False), (date(2031, 4, 22), True), (date(2031, 7, 2), False)])
    assert kept == [date(2031, 4, 22), date(2031, 7, 2)]


# ── the next print, estimated ───────────────────────────────────────────────
from datetime import timedelta  # noqa: E402

from bifrost_research.repositories.earnings_filings import distinct_prints, expected_next  # noqa: E402

# Invented quarterly prints; 2031's land on 2030's plus 52 weeks but for Aug (two days late).
QUARTERS = [
    date(2030, 2, 4), date(2030, 5, 6), date(2030, 8, 5), date(2030, 11, 4),
    date(2031, 2, 3), date(2031, 5, 5), date(2031, 8, 6), date(2031, 11, 3),
]


def test_amendments_collapse_into_one_print() -> None:
    assert distinct_prints([date(2031, 8, 6), date(2031, 8, 8), date(2031, 5, 5)]) == [date(2031, 5, 5), date(2031, 8, 6)]


def test_the_next_print_is_last_years_same_quarter_plus_52_weeks() -> None:
    out = expected_next(QUARTERS, as_of=date(2031, 11, 20))
    assert out is not None
    assert out["date"] == (date(2031, 2, 3) + timedelta(days=364)).isoformat() == "2032-02-02"
    assert out["from"] == "2031-02-03" and out["days_away"] == 74
    # 2031's four prints against 2030's plus 364 days: 0, 0, 2, 0 days off.
    assert out["track"] == {"n": 4, "median_miss_days": 0.0, "max_miss_days": 2}


def test_an_estimate_that_has_passed_says_so_by_its_sign() -> None:
    out = expected_next(QUARTERS, as_of=date(2032, 2, 9))
    assert out is not None and out["days_away"] == -7


def test_an_estimate_long_past_is_withdrawn() -> None:
    # Two weeks past the estimate is still a late print; beyond that the name has stopped.
    assert expected_next(QUARTERS, as_of=date(2032, 2, 16)) is not None
    assert expected_next(QUARTERS, as_of=date(2032, 2, 17)) is None


def test_no_estimate_without_four_quarterly_prints() -> None:
    assert expected_next(QUARTERS[:3], as_of=date(2031, 1, 1)) is None
    # A year-long hole before the last print breaks the cadence.
    gappy = [date(2030, 2, 4), date(2030, 5, 6), date(2030, 8, 5), date(2031, 8, 4)]
    assert expected_next(gappy, as_of=date(2031, 9, 1)) is None
