"""Narrative lens — the deterministic readings. Fixtures are invented."""

from __future__ import annotations

from datetime import date

from bifrost_research.lenses.narrative import (
    SEC_ITEM_READING,
    item_quote,
    sec_item_tags,
    vendor_tag,
)


def _filing(**over):
    base = {
        "accession_number": "0000000000-31-000001",
        "symbol": "ZZZ",
        "filing_date": date(2031, 3, 11),
        "items": ["2.02", "9.01"],
        "items_text": "Item 2.02 Results of Operations and Financial Condition.\n\nOn March 11, 2031, Zeta Corp issued a press release.\n\nItem 9.01 Financial Statements and Exhibits. (d) Exhibits.",
        "filing_url": "https://www.sec.gov/Archives/edgar/data/1/0000000000-31-000001.txt",
    }
    base.update(over)
    return base


def test_one_reading_per_substantive_item_and_exhibits_are_not_one():
    tags = sec_item_tags(_filing())
    assert [t["item"] for t in tags] == ["2.02"]
    t = tags[0]
    assert t["basis"] == "sec"
    assert t["kind"] == "event"
    assert t["reading"] == SEC_ITEM_READING["2.02"]


def test_the_quote_opens_at_the_items_own_heading_whatever_the_spacing():
    body = "Item 1.01 Entry into an agreement. Terms follow.  ITEM 5.02. Departure of an officer.\nMore."
    assert item_quote(body, "5.02").startswith("ITEM 5.02. Departure of an officer.")
    # The body is flattened: a filing's newlines never reach a row.
    assert "\n" not in item_quote(body, "5.02")


def test_a_missing_heading_falls_back_to_the_bodys_opening_and_is_clipped():
    body = "word " * 200
    q = item_quote(body, "8.01")
    assert q.endswith("…")
    assert len(q) <= 262


def test_an_unknown_item_still_names_itself():
    assert sec_item_tags(_filing(items=["6.05"]))[0]["reading"] == "item 6.05"


def test_the_vendor_reading_is_its_most_specific_label():
    t = vendor_tag(
        {
            "accession_number": "a",
            "symbol": "ZZZ",
            "filing_date": date(2031, 3, 11),
            "filing_url": None,
            "primary_category": "leadership_and_governance",
            "secondary_category": "executive_leadership",
            "tertiary_category": "executive_officer_departure",
            "supporting_text": "An officer   stepped down.\n",
        }
    )
    assert t["basis"] == "vendor"
    assert t["reading"] == "executive officer departure"
    assert t["category"] == "leadership and governance"
    assert t["quote"] == "An officer stepped down."
    assert t["item"] is None
