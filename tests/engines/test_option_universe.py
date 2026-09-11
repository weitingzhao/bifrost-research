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


# ── the ingested route admits index roots only (0.102.0) ──────────────────
#
# Until 0.102.0 every underlying in raw_market.option_contract was resident.
# The Plugin ingests every name in this table, so the rule absorbed its own
# output: on 2026-09-11 one refresh moved 548 names from core/edge to
# resident, and the Plugin snapshots resident chains whole (~187,000
# contracts a session would have become 716,787).


class _FakeCursor:
    def __init__(self, calls: list, answers: dict) -> None:
        self.calls, self.answers, self._rows = calls, answers, []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.calls.append((sql, params))
        self._rows = self.answers["watchlist" if "watchlist_cache" in sql else "contract"]

    def fetchall(self) -> list:
        return self._rows


class _FakeConn:
    def __init__(self, answers: dict) -> None:
        self.calls: list = []
        self.answers = answers

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.calls, self.answers)

    def rollback(self) -> None:
        return None


def test_the_ingested_route_asks_only_for_index_roots() -> None:
    conn = _FakeConn({"watchlist": [("NVDA",)], "contract": [("SPX",)]})
    resident = ou.load_resident(conn)
    contract_sql, params = next(c for c in conn.calls if "option_contract" in c[0])
    assert "ANY(%s)" in contract_sql
    assert set(params[0]) == ou.INDEX_OPTION_ROOTS
    assert resident == {"SPY": "benchmark", "QQQ": "benchmark", "IWM": "benchmark",
                        "NVDA": "watchlist", "SPX": "ingested"}


def test_the_index_roots_are_indices_not_stocks() -> None:
    assert {"SPX", "SPXW", "NDX", "RUT", "VIX"} <= ou.INDEX_OPTION_ROOTS
    assert not ou.INDEX_OPTION_ROOTS & {"SPY", "QQQ", "IWM", "AAPL", "AVB", "SATS"}


def _promoted(days_ago: int = 0) -> dict:
    """A row the old route made resident: reason 'ingested', placed on the refresh."""
    return {"tier": "resident", "entered_on": D - timedelta(days=days_ago),
            "last_seen": D - timedelta(days=days_ago), "history_months": 24, "reason": "ingested"}


def test_a_promoted_name_still_liquid_returns_to_core() -> None:
    out = build(existing={"A": _promoted()}, liquidity={"A": 3e8})
    assert out["A"]["tier"] == "core"


def test_a_promoted_name_in_the_hysteresis_band_keeps_core() -> None:
    # 1.2e8 <= dv < 2e8: a fresh name would not enter, a core name would stay.
    out = build(existing={"B": _promoted()}, liquidity={"B": 1.5e8})
    assert out["B"]["tier"] == "core"


def test_a_promoted_name_below_the_exit_floor_steps_down_to_edge() -> None:
    out = build(existing={"C": _promoted()}, liquidity={"C": 1e7})
    assert out["C"]["tier"] == "edge"
    assert out["C"]["reason"] == "stepped-down:ingested"
    assert out["C"]["history_months"] == ou.EDGE_HISTORY_MONTHS


def test_a_stepped_down_name_then_ages_out_like_any_edge_name() -> None:
    stale = _promoted(days_ago=ou.EDGE_RETENTION_DAYS + 1)
    assert "D" not in build(existing={"D": stale})


def test_an_index_root_stays_resident() -> None:
    out = build(existing={"SPX": _promoted()}, resident={"SPX": "ingested"})
    assert out["SPX"]["tier"] == "resident"


def test_a_watchlist_resident_still_leaves_when_dropped() -> None:
    # The step-down is for the route being narrowed, not a new rule for residents.
    prev = dict(_promoted(), reason="watchlist")
    assert "W" not in build(existing={"W": prev}, liquidity={"W": 1e7})
