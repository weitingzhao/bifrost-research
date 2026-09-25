"""Narrative lens — the deterministic half (Vision §9.2).

What the text says, structured, in its own column: never mixed into a
measured score. Two kinds of reading exist today, both *deterministic*:

- **SEC item** — every 8-K is indexed by the item numbers it files under
  (1.01 a material agreement, 2.02 results, 5.02 an officer change …). The
  filing states it; nothing is inferred, so there is no confidence to report.
- **Vendor classification** — the data vendor labels a share of 8-Ks with a
  three-level category. A second label where it exists, never the index.

The model readings the design also draws — 10-K risk-factor changes year over
year, supply language in MD&A, each with the model's confidence — need an
extractor that does not exist. Their source text is ingested
(`raw_market.sec_10k_section`); the readings are owed, and the API says so
rather than inventing them.

Item 9.01 (financial statements and exhibits) is filed alongside nearly every
8-K and says nothing on its own, so it is not a reading.
"""

from __future__ import annotations

import re
from typing import Any

# Form 8-K items, in the SEC's own words, shortened to what a row can hold.
SEC_ITEM_READING: dict[str, str] = {
    "1.01": "material agreement entered",
    "1.02": "material agreement terminated",
    "1.03": "bankruptcy or receivership",
    "1.04": "mine safety",
    "1.05": "material cybersecurity incident",
    "2.01": "acquisition or disposition completed",
    "2.02": "results of operations filed",
    "2.03": "direct financial obligation created",
    "2.04": "obligation accelerated",
    "2.05": "exit or disposal costs",
    "2.06": "material impairment",
    "3.01": "delisting notice or listing-rule failure",
    "3.02": "unregistered sale of equity",
    "3.03": "security holder rights modified",
    "4.01": "certifying accountant changed",
    "4.02": "prior financials no longer reliable",
    "5.01": "change in control",
    "5.02": "officer or director departure / appointment",
    "5.03": "articles or bylaws amended",
    "5.04": "benefit-plan trading suspended",
    "5.05": "code of ethics amended",
    "5.06": "shell company status changed",
    "5.07": "shareholder vote results",
    "5.08": "shareholder director nominations",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
}

EXCLUDED_ITEMS = frozenset({"9.01"})

QUOTE_CHARS = 260


def collapse(text: str | None) -> str:
    """One line: the filing bodies carry hundreds of newlines and table rules."""
    return " ".join(str(text or "").split())


def clip(text: str, n: int = QUOTE_CHARS) -> str:
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return f"{cut}…"


def item_quote(items_text: str | None, item: str) -> str:
    """The span that opens the item's own section, else the body's opening.

    The body writes the heading several ways (`Item 2.02`, a thin space between
    the word and the number, `ITEM 2.02.`), so the match allows any whitespace
    between the word and the number and ignores case.
    """
    body = collapse(items_text)
    if not body:
        return ""
    m = re.search(r"item\s*" + re.escape(item) + r"\b", body, flags=re.IGNORECASE)
    return clip(body[m.start():] if m else body)


def humanize(label: str | None) -> str:
    return str(label or "").replace("_", " ").strip()


def sec_item_tags(filing: dict[str, Any]) -> list[dict[str, Any]]:
    """One reading per substantive item a filing lists."""
    out: list[dict[str, Any]] = []
    for item in filing.get("items") or []:
        item = str(item).strip()
        if not item or item in EXCLUDED_ITEMS:
            continue
        out.append(
            {
                "symbol": filing["symbol"],
                "kind": "event",
                "basis": "sec",
                "item": item,
                "category": None,
                "reading": SEC_ITEM_READING.get(item, f"item {item}"),
                "quote": item_quote(filing.get("items_text"), item),
                "form": "8-K",
                "filing_date": filing["filing_date"],
                "filing_url": filing.get("filing_url"),
                "accession": filing["accession_number"],
            }
        )
    return out


def vendor_tag(row: dict[str, Any]) -> dict[str, Any]:
    """The vendor's most specific label as the reading, its span as the quote."""
    reading = humanize(row.get("tertiary_category")) or humanize(row.get("secondary_category"))
    primary = humanize(row.get("primary_category"))
    return {
        "symbol": row["symbol"],
        "kind": "event",
        "basis": "vendor",
        "item": None,
        "category": primary or None,
        "reading": reading or primary or "classified",
        "quote": clip(collapse(row.get("supporting_text"))),
        "form": "8-K",
        "filing_date": row["filing_date"],
        "filing_url": row.get("filing_url"),
        "accession": row["accession_number"],
    }
