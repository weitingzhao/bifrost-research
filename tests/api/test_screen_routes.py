"""Screening routes — coverage is the calibration number, live.

The number that used to be measured by hand and written into a document ("0 of
575 names have all six faces") moves every night as the collector widens, so it
has to be readable, not transcribed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from bifrost_research.api import screen as mod
from bifrost_research.lenses.registry import LENSES
from bifrost_research.lenses.screen import LensReading, ScreenResult, ScreenRow


def _row(
    symbol: str, found: dict[str, tuple[Any, str | None]], asked: tuple[str, ...]
) -> ScreenRow:
    return ScreenRow(
        symbol=symbol,
        readings={
            lens: LensReading(lens=lens, value=v, band=b, as_of=date(2026, 9, 8))
            for lens, (v, b) in found.items()
        },
        missing=tuple(x for x in asked if x not in found and x not in mod_unscreenable()),
        survived=True,
    )


def mod_unscreenable() -> dict[str, str]:
    return {"skew": "weeks of history", "term_slope": "no set-based reader yet"}


@pytest.fixture()
def fake_screen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": []}

    def _screen(conn: Any, **kwargs: Any) -> ScreenResult:
        state["calls"].append(kwargs)
        asked = tuple(kwargs["lenses"])
        readable = tuple(x for x in asked if x not in mod_unscreenable())
        rows = (
            _row("NVDA", {lens: (50.0, "neutral") for lens in readable}, asked),
            _row("MSFT", {"sepa": (84.0, "hot")} if "sepa" in readable else {}, asked),
        )
        if kwargs.get("require"):
            rows = tuple(
                ScreenRow(
                    symbol=r.symbol,
                    readings=r.readings,
                    missing=r.missing,
                    survived=all(
                        lens in r.readings and r.readings[lens].band in bands
                        for lens, bands in kwargs["require"].items()
                    ),
                )
                for r in rows
            )
        return ScreenResult(
            lenses=asked,
            universe=("NVDA", "MSFT"),
            rows=rows,
            coverage={lens: sum(1 for r in rows if lens in r.readings) for lens in readable},
            unscreenable={k: v for k, v in mod_unscreenable().items() if k in asked},
        )

    monkeypatch.setattr(mod, "screen", _screen)
    monkeypatch.setattr(mod, "connect", lambda: object())
    return state


def test_coverage_reports_every_face_and_the_roll_up(fake_screen: dict[str, Any]) -> None:
    body = mod.get_coverage(tiers=None)["data"]

    assert body["universe"] == 2
    assert body["tiers"] == ["resident", "core", "edge"]
    by_lens = {row["lens"]: row for row in body["lenses"]}
    assert set(by_lens) == set(mod.ALL_LENSES)
    assert by_lens["sepa"]["face"] == "trend" and by_lens["sepa"]["read"] == 2
    assert by_lens["iv_rank"]["face"] == "volatility" and by_lens["iv_rank"]["read"] == 1
    # A lens that cannot be screened says why, and carries no count.
    assert by_lens["skew"]["read"] is None
    assert "history" in by_lens["skew"]["unscreenable"]
    # The roll-up: NVDA reads every screenable lens, MSFT only the stock side.
    assert body["every_face"] == 1
    assert body["no_option_face"] == 1
    assert body["screenable_lenses"] == len(mod.ALL_LENSES) - 2


def test_coverage_can_be_asked_of_one_tier(fake_screen: dict[str, Any]) -> None:
    mod.get_coverage(tiers="core, edge")
    assert fake_screen["calls"][-1]["tiers"] == ["core", "edge"]

    with pytest.raises(mod.HTTPException) as err:
        mod.get_coverage(tiers="core,nope")
    assert err.value.status_code == 400 and "nope" in err.value.detail


def test_a_screen_returns_the_names_that_pass_with_what_they_lack(
    fake_screen: dict[str, Any],
) -> None:
    body = mod.get_screen(lenses="sepa,iv_rank", require="sepa:hot", tiers=None, limit=10)["data"]

    assert body["require"] == {"sepa": ["hot"]}
    assert body["survivors"] == 1
    row = body["rows"][0]
    assert row["symbol"] == "MSFT"
    assert row["readings"]["sepa"] == {"value": 84.0, "band": "hot", "as_of": "2026-09-08"}
    assert row["missing"] == ["iv_rank"]  # it passed on the stock side and says so
    assert body["truncated"] is False


def test_bands_may_be_alternatives_and_unknown_names_are_refused(
    fake_screen: dict[str, Any],
) -> None:
    mod.get_screen(lenses="iv_rank", require="iv_rank:hot|lean_hot", tiers=None, limit=10)
    assert fake_screen["calls"][-1]["require"] == {"iv_rank": ["hot", "lean_hot"]}

    for bad in (dict(lenses="nope", require=None), dict(lenses="sepa", require="nope:hot")):
        with pytest.raises(mod.HTTPException) as err:
            mod.get_screen(tiers=None, limit=10, **bad)
        assert err.value.status_code == 400


def test_every_registry_lens_belongs_to_exactly_one_face() -> None:
    assert set(mod.ALL_LENSES) == set(LENSES)
    assert len(mod.ALL_LENSES) == len(set(mod.ALL_LENSES))
