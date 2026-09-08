"""The composite batch — the Dossier's twelve lenses in one request.

Twelve separate exhibit requests each opened their own connection, and a
connection costs about as much as reading a lens. The batch fans the lenses
across a few workers instead, and must still answer in the order it was asked,
with a lens that fails saying so rather than sinking its neighbours.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from bifrost_research.api import exhibit as mod


class _FakeConn:
    def __init__(self, ident: int) -> None:
        self.ident = ident
        self.closed = False
        self.rolled_back = 0

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


class _Exhibit:
    """Just enough of ExhibitResponse for the route to serialise."""

    def __init__(self, lens: str, symbol: str, conn: _FakeConn) -> None:
        self.lens, self.symbol, self.conn = lens, symbol, conn

    def model_dump(self) -> dict[str, Any]:
        return {"lens": self.lens, "symbol": self.symbol, "conn": self.conn.ident}


@pytest.fixture()
def wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"conns": [], "built": [], "fail": set(), "lock": threading.Lock()}

    def fake_connect() -> _FakeConn:
        with state["lock"]:
            conn = _FakeConn(len(state["conns"]))
            state["conns"].append(conn)
            return conn

    def fake_build(conn: _FakeConn, lens: str, symbol: str) -> _Exhibit:
        with state["lock"]:
            state["built"].append(lens)
        if lens in state["fail"]:
            raise RuntimeError(f"{lens} is out")
        return _Exhibit(lens, symbol, conn)

    monkeypatch.setattr(mod, "connect", fake_connect)
    monkeypatch.setattr(mod, "build_exhibit", fake_build)
    monkeypatch.setattr(
        mod,
        "exhibit_lens_names",
        lambda: {"vrp", "iv_rank", "skew", "sepa", "gex_regime", "opex_pin"},
    )
    return state


ALL = "vrp,iv_rank,skew,sepa,gex_regime,opex_pin"


def test_answers_in_the_order_asked_on_a_handful_of_connections(wiring: dict[str, Any]) -> None:
    body = mod.get_exhibit_composite(symbol=" nvda ", lenses=ALL)

    assert body["ok"] is True
    assert body["data"]["symbol"] == "NVDA"
    assert body["data"]["lenses"] == ALL.split(",")
    assert [e["lens"] for e in body["data"]["exhibits"]] == ALL.split(",")
    # Six lenses, four workers: four connections, not six — and each one closed.
    assert len(wiring["conns"]) == mod.MAX_COMPOSITE_WORKERS
    assert all(c.closed for c in wiring["conns"])
    # Every lens was read exactly once, each on the connection of its own worker.
    assert sorted(wiring["built"]) == sorted(ALL.split(","))
    assert len({e["conn"] for e in body["data"]["exhibits"]}) == mod.MAX_COMPOSITE_WORKERS


def test_a_lens_that_fails_says_so_and_the_rest_still_answer(wiring: dict[str, Any]) -> None:
    wiring["fail"] = {"skew"}

    exhibits = mod.get_exhibit_composite(symbol="NVDA", lenses=ALL)["data"]["exhibits"]

    by_lens = {e["lens"]: e for e in exhibits}
    assert len(exhibits) == 6
    assert by_lens["skew"]["freshness"] == "missing"
    assert "skew is out" in by_lens["skew"]["caveats"][0]
    assert by_lens["vrp"]["conn"] is not None  # its neighbours are untouched
    assert sum(c.rolled_back for c in wiring["conns"]) == 1


def test_unknown_lenses_are_dropped_and_a_repeat_is_read_once(wiring: dict[str, Any]) -> None:
    body = mod.get_exhibit_composite(symbol="NVDA", lenses="vrp, nope ,vrp,skew")

    assert body["data"]["lenses"] == ["vrp", "skew"]
    assert wiring["built"].count("vrp") == 1


def test_one_lens_needs_no_second_connection(wiring: dict[str, Any]) -> None:
    body = mod.get_exhibit_composite(symbol="NVDA", lenses="vrp")

    assert [e["lens"] for e in body["data"]["exhibits"]] == ["vrp"]
    assert len(wiring["conns"]) == 1


def test_a_database_that_is_down_is_a_503_not_twelve_excuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> Any:
        raise RuntimeError("no route to host")

    monkeypatch.setattr(mod, "connect", refuse)
    with pytest.raises(mod.HTTPException) as err:
        mod.get_exhibit_composite(symbol="NVDA", lenses=ALL)
    assert err.value.status_code == 503


def test_a_worker_that_cannot_connect_stubs_only_its_own_share(
    wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = mod.connect
    calls = {"n": 0}

    def flaky() -> Any:
        calls["n"] += 1
        if calls["n"] == 3:  # the third connection — one worker, not the probe
            raise RuntimeError("pool exhausted")
        return real()

    monkeypatch.setattr(mod, "connect", flaky)
    exhibits = mod.get_exhibit_composite(symbol="NVDA", lenses=ALL)["data"]["exhibits"]

    missing = [e["lens"] for e in exhibits if e.get("freshness") == "missing"]
    assert 0 < len(missing) < 6
    assert all(
        "pool exhausted" in e["caveats"][0] for e in exhibits if e.get("freshness") == "missing"
    )
