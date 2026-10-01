"""Two documents, one rule: the blueprint says what should be, the calibration
says what is. Status glyphs in the blueprint would be the two collapsing back
into one page, which is what the Owner asked to be kept apart."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bifrost_research.api import docs
from bifrost_research.api.app import create_app

STATUS_GLYPHS = ("✅", "⚠️", "❌", "⏳")


def test_both_documents_are_served_with_front_matter_parsed() -> None:
    client = TestClient(create_app())
    for slug, first_line in (("blueprint", "# Research 蓝图"), ("calibration", "# Research 校准")):
        r = client.get(f"/research/docs/{slug}")
        assert r.status_code == 200, slug
        d = r.json()["data"]
        assert d["slug"] == slug
        assert d["version"] and d["updated"], slug
        assert not d["markdown"].lstrip().startswith("---"), slug
        assert d["markdown"].lstrip().startswith(first_line), slug


def test_the_blueprint_defines_the_contracts_and_carries_no_status() -> None:
    body = docs.read_doc("blueprint")["markdown"]
    for anchor in ("C-F1", "C-R1", "C-A1", "C-A7", "C-C1", "C-C5", "C-U1", "## 3. 理想的宽与深", "## 4. 契约"):
        assert anchor in body, anchor
    for glyph in STATUS_GLYPHS:
        assert glyph not in body, f"status glyph {glyph} belongs in the calibration, not the blueprint"


def test_the_calibration_reports_status_against_every_blueprint_contract() -> None:
    import re

    blueprint = docs.read_doc("blueprint")["markdown"]
    calibration = docs.read_doc("calibration")["markdown"]
    defined = set(re.findall(r"\| (C-[FRACU]\d+) \|", blueprint))
    reported = set(re.findall(r"\| (C-[FRACU]\d+) \|", calibration))
    assert defined, "no contracts defined"
    assert defined <= reported, f"contracts with no status: {sorted(defined - reported)}"
    assert any(g in calibration for g in STATUS_GLYPHS)


def test_the_calibration_roll_up_matches_its_rows() -> None:
    """The §2 tally is what the Lab calibration page compares its own count against.

    It said 27 contracts and ✅15 ⚠️5 ❌6 ⏳1 from the first calibration on while
    the rows said 28 and, by 2026-10-01, ✅16 ⚠️5 ❌5 ⏳2: four row edits on
    2026-09-08 never reached the tally. Counted the way the page counts it — the
    rows of §2 whose first cell is a contract id, by the glyph opening the second.
    """
    import re
    from collections import Counter

    calibration = docs.read_doc("calibration")["markdown"]
    section = re.search(r"^## 2\..*?(?=^## )", calibration, re.M | re.S)
    assert section, "no §2"
    rows = re.findall(r"^\| (C-[A-Z]\d+) \| (✅|⚠️?|❌|⏳)", section.group(0), re.M)
    by_glyph = Counter(g.rstrip("\ufe0f") for _, g in rows)
    tally = re.search(
        r"\*\*计数\*\*[：:]\s*✅\s*(\d+)\s*⚠️?\s*(\d+)\s*❌\s*(\d+)\s*⏳\s*(\d+)，共\s*(\d+)\s*条",
        section.group(0),
    )
    assert tally, "no roll-up line in §2"
    ok, warn, fail, ramp, total = (int(x) for x in tally.groups())
    assert (ok, warn, fail, ramp) == (by_glyph["✅"], by_glyph["⚠"], by_glyph["❌"], by_glyph["⏳"])
    assert total == len(rows) == ok + warn + fail + ramp


def test_an_unknown_document_is_a_404_not_a_directory_listing() -> None:
    client = TestClient(create_app())
    assert client.get("/research/docs/../../pyproject").status_code in (404, 422)
    assert client.get("/research/docs/nope").status_code == 404
