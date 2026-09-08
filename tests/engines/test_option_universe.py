"""Three tiers, two rules, no hand list — the rule as pure logic.

The option universe used to be whatever the Plugin had ingested. These pin
the rule that replaces it: precedence between tiers, the hysteresis that keeps
a core name's history from fragmenting at the threshold, and the retention
that keeps an edge name long enough for its hits to settle.
"""

from __future__ import annotations

from datetime import date, timedelta

from bifrost_research.engines.option_universe import entry as ou

D = date(2026, 9, 8)


def build(**kw):
    base = dict(as_of=D, existing={}, resident={}, liquidity={}, edge_candidates=set())
    base.update(kw)
    return ou.build_universe(**base)


# ── precedence ────────────────────────────────────────────────────────────


def test_resident_outranks_core_outranks_edge() -> None:
    out = build(
        resident={"NVDA": "watchlist"},
        liquidity={"NVDA": 5e9, "AAPL": 5e9},
        edge_candidates={"NVDA", "AAPL", "HALO"},
    )
    assert out["NVDA"]["tier"] == "resident"
    assert out["AAPL"]["tier"] == "core"
    assert out["HALO"]["tier"] == "edge"


def test_benchmarks_are_always_resident() -> None:
    out = build()
    assert {s for s, r in out.items() if r["tier"] == "resident"} == set()  # resident comes from the loader
    resident = {s: "benchmark" for s in ou.RESIDENT_BENCHMARKS}
    out = build(resident=resident)
    assert all(out[s]["tier"] == "resident" for s in ou.RESIDENT_BENCHMARKS)


# ── core hysteresis ───────────────────────────────────────────────────────


def test_core_enters_at_the_floor_and_not_below() -> None:
    out = build(liquidity={"A": ou.CORE_ENTER_USD, "B": ou.CORE_ENTER_USD - 1})
    assert "A" in out and out["A"]["tier"] == "core"
    assert "B" not in out


def test_a_core_name_stays_until_it_falls_below_the_exit_floor() -> None:
    prev = {"tier": "core", "entered_on": D - timedelta(days=40), "last_seen": D - timedelta(days=1),
            "history_months": 24, "reason": "x"}
    between = (ou.CORE_ENTER_USD + ou.CORE_EXIT_USD) / 2
    out = build(existing={"A": prev}, liquidity={"A": between})
    assert out["A"]["tier"] == "core"
    assert out["A"]["entered_on"] == prev["entered_on"], "membership continued, so the entry date is kept"
    out = build(existing={"A": prev}, liquidity={"A": ou.CORE_EXIT_USD - 1})
    assert "A" not in out


def test_a_non_member_between_the_floors_does_not_enter() -> None:
    between = (ou.CORE_ENTER_USD + ou.CORE_EXIT_USD) / 2
    assert "A" not in build(liquidity={"A": between})


# ── edge entry, retention, expiry ─────────────────────────────────────────


def test_an_edge_name_enters_with_a_year_and_a_core_name_with_two() -> None:
    out = build(liquidity={"A": 5e9}, edge_candidates={"H"})
    assert out["A"]["history_months"] == ou.CORE_HISTORY_MONTHS
    assert out["H"]["history_months"] == ou.EDGE_HISTORY_MONTHS


def test_an_edge_name_not_seen_today_is_kept_within_retention() -> None:
    prev = {"tier": "edge", "entered_on": D - timedelta(days=30), "last_seen": D - timedelta(days=30),
            "history_months": 12, "reason": "sepa>=70"}
    out = build(existing={"H": prev})
    assert out["H"] == prev


def test_an_edge_name_expires_after_retention() -> None:
    prev = {"tier": "edge", "entered_on": D - timedelta(days=200), "last_seen": D - timedelta(days=ou.EDGE_RETENTION_DAYS + 1),
            "history_months": 12, "reason": "sepa>=70"}
    assert "H" not in build(existing={"H": prev})


def test_an_edge_name_seen_again_refreshes_last_seen_and_keeps_entered_on() -> None:
    prev = {"tier": "edge", "entered_on": D - timedelta(days=30), "last_seen": D - timedelta(days=30),
            "history_months": 12, "reason": "sepa>=70"}
    out = build(existing={"H": prev}, edge_candidates={"H"})
    assert out["H"]["last_seen"] == D
    assert out["H"]["entered_on"] == prev["entered_on"]


def test_a_name_promoted_from_edge_to_core_gets_a_fresh_entry_date() -> None:
    prev = {"tier": "edge", "entered_on": D - timedelta(days=30), "last_seen": D - timedelta(days=1),
            "history_months": 12, "reason": "sepa>=70"}
    out = build(existing={"H": prev}, liquidity={"H": 5e9})
    assert out["H"]["tier"] == "core"
    assert out["H"]["entered_on"] == D
    assert out["H"]["history_months"] == ou.CORE_HISTORY_MONTHS


def test_a_resident_name_dropped_from_the_watchlist_leaves() -> None:
    prev = {"tier": "resident", "entered_on": D - timedelta(days=30), "last_seen": D - timedelta(days=1),
            "history_months": 24, "reason": "watchlist"}
    assert "X" not in build(existing={"X": prev})
