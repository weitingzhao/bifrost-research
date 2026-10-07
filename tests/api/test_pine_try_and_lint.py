"""Pine workbench (S12/S13, Owner 2026-10-06): saves and checks refused with the lines; try a basket."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.engines.pine import client, trial
from bifrost_research.engines.pine.library import PineScript

_MOD = "bifrost_research.api.pine"
_SRC = '//@version=5\nindicator("t")\nplotshape(close > open, "buy")'
_BAD = '//@version=5\nindicator("t")\na = close +\nplotshape(a > 0, "buy")'
_ISSUE = {"line": 3, "col": 11, "message": "line 3 ends with `+` but the next line does not continue it (indent the continuation)"}


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(f"{_MOD}.connect", lambda: MagicMock())
    return TestClient(create_app())


# -- client ------------------------------------------------------------------------


def _http_error(status: int, body: dict[str, Any]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://runner/run", status, "x", {}, io.BytesIO(json.dumps(body).encode()))


def test_client_raises_the_problems_with_their_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(req, timeout):  # noqa: ANN001
        raise _http_error(400, {"ok": False, "error": "the script has 1 problem; line 3: …", "issues": [_ISSUE]})

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(client.PineScriptProblems) as exc:
        client.run(_BAD, {"AAA": [{"date": date(2026, 1, 5), "close": 1.0}]})
    assert exc.value.issues == [_ISSUE] and "line 3" in str(exc.value)

    def plain(req, timeout):  # noqa: ANN001
        raise _http_error(400, {"ok": False, "error": "source is required"})

    monkeypatch.setattr("urllib.request.urlopen", plain)
    with pytest.raises(ValueError) as exc2:
        client.lint("")
    assert not isinstance(exc2.value, client.PineScriptProblems)


def test_client_lint_and_error_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    def post(path, payload, timeout):  # noqa: ANN001
        if path == "/lint":
            return {"ok": True, "issues": [_ISSUE]}
        return {"ok": True, "results": [{"symbol": "AAA", "error": "foo is not defined", "line": 4, "col": 5}]}

    monkeypatch.setattr(client, "_post", post)
    assert client.lint(_BAD) == [_ISSUE]
    out = client.run(_SRC, {"AAA": [{"date": date(2026, 1, 5), "close": 1.0}]})
    assert out["AAA"] == {"error": "foo is not defined", "line": 4, "col": 5}


# -- save and check ------------------------------------------------------------------


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_MOD}.get_script", lambda conn, sid: None)
    monkeypatch.setattr(f"{_MOD}.upsert_script", lambda conn, s: s)


def test_a_save_with_problems_is_refused_with_the_lines(api: TestClient, owner: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_MOD}.client.lint", lambda src: [_ISSUE])
    r = api.put("/research/pine/scripts/my_try", json={"name": "t", "source": _BAD})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("the script has 1 problem; line 3:")
    assert r.json()["issues"] == [_ISSUE]


def test_a_clean_save_goes_through_and_an_unreachable_runner_refuses_it(api: TestClient, owner: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_MOD}.client.lint", lambda src: [])
    assert api.put("/research/pine/scripts/my_try", json={"name": "t", "source": _SRC}).status_code == 200

    def down(src):  # noqa: ANN001
        raise OSError("connection refused")

    monkeypatch.setattr(f"{_MOD}.client.lint", down)
    r = api.put("/research/pine/scripts/my_try", json={"name": "t", "source": _SRC})
    assert r.status_code == 503 and "cannot be checked now" in r.json()["detail"]


def test_check_returns_the_lines_of_a_refused_or_failing_script(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_MOD}.load_bars", lambda conn, sym, a, b: [{"date": date(2026, 1, 5), "close": 1.0}])

    def refused(source, series, **kw):  # noqa: ANN001
        raise client.PineScriptProblems("the script has 1 problem; line 3: …", [_ISSUE])

    monkeypatch.setattr(f"{_MOD}.client.run", refused)
    r = api.post("/research/pine/check", json={"source": _BAD, "symbol": "SPY"})
    assert r.status_code == 400 and r.json()["issues"] == [_ISSUE]

    monkeypatch.setattr(f"{_MOD}.client.run", lambda source, series, **kw: {"SPY": {"error": "foo is not defined", "line": 4, "col": 5}})
    r = api.post("/research/pine/check", json={"source": _SRC, "symbol": "SPY"})
    assert r.status_code == 400
    assert r.json() == {"detail": "script error: foo is not defined", "issues": [{"line": 4, "col": 5, "message": "foo is not defined"}]}


# -- try a basket ----------------------------------------------------------------------


def test_try_defaults_to_the_resident_basket_and_returns_signals_and_stats(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(f"{_MOD}.trial.basket_symbols", lambda conn, b: seen.setdefault("basket", b) and ["SPY", "QQQ"])

    def run_trial(conn, source, symbols, **kw):  # noqa: ANN001
        seen.update(symbols=list(symbols), **kw)
        return {"context": [], "symbols": [{"symbol": "SPY", "buy": 3, "sell": 1}], "stats": {"buy": {"signals": 3}, "sell": {"signals": 1}}}

    monkeypatch.setattr(f"{_MOD}.trial.run_trial", run_trial)
    r = api.post("/research/pine/try", json={"source": _SRC, "days": 365})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert seen["basket"] == "resident" and seen["symbols"] == ["SPY", "QQQ"] and seen["horizons"] == [5, 10, 20]
    assert (seen["end"] - seen["start"]).days == 365
    assert d["basket"] == "resident" and d["stats"]["buy"]["signals"] == 3 and d["symbols"][0]["symbol"] == "SPY"


@pytest.mark.parametrize(
    "body",
    [
        {"symbols": ["SPY"]},
        {"source": _SRC, "script": "supertrend", "symbols": ["SPY"]},
        {"source": _SRC, "symbols": ["SPY"], "basket": "resident"},
        {"source": _SRC, "symbols": [f"S{i}" for i in range(51)]},
        {"source": _SRC, "days": 30},
        {"source": _SRC, "basket": "everything"},
    ],
)
def test_try_bad_bodies_are_422(api: TestClient, body: dict[str, Any]) -> None:
    assert api.post("/research/pine/try", json=body).status_code == 422


def test_try_a_library_script_and_refused_scripts(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_MOD}.get_script", lambda conn, sid: PineScript(id=sid, name="s", source=_SRC, version=3) if sid == "mine" else None)
    monkeypatch.setattr(f"{_MOD}.trial.run_trial", lambda conn, source, symbols, **kw: {"context": [], "symbols": [], "stats": {}})
    d = api.post("/research/pine/try", json={"script": "mine", "symbols": ["spy"]}).json()["data"]
    assert (d["script"], d["script_version"], d["basket"]) == ("mine", 3, None)
    assert api.post("/research/pine/try", json={"script": "nope", "symbols": ["SPY"]}).status_code == 404

    def refused(conn, source, symbols, **kw):  # noqa: ANN001
        raise client.PineScriptProblems("the script has 1 problem; line 3: …", [_ISSUE])

    monkeypatch.setattr(f"{_MOD}.trial.run_trial", refused)
    r = api.post("/research/pine/try", json={"source": _BAD, "symbols": ["SPY"]})
    assert r.status_code == 400 and r.json()["issues"] == [_ISSUE]


def test_run_trial_measures_the_signals_the_build_would_store(monkeypatch: pytest.MonkeyPatch) -> None:
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(400)]
    bars = {"AAA": [{"date": d, "close": 10.0} for d in days], "BBB": [{"date": d, "close": 5.0} for d in days]}
    monkeypatch.setattr(trial.build, "load_bars_many", lambda conn, syms, a, b: {s: bars[s] for s in syms if s in bars})
    monkeypatch.setattr(
        trial.client,
        "run",
        lambda source, b, **kw: {
            "AAA": {"buy": [days[50], days[150], days[350]], "sell": [days[200]]},
            "BBB": {"error": "foo is not defined", "line": 4, "col": 5},
        },
    )
    calls: list[dict[str, Any]] = []

    def evaluate(conn, by_sym, **kw):  # noqa: ANN001
        calls.append({"by_sym": by_sym, **kw})
        return {"signals": sum(len(v) for v in by_sym.values())}

    monkeypatch.setattr(trial, "evaluate", evaluate)
    out = trial.run_trial(None, _SRC, ["aaa", "BBB", "CCC"], start=days[120], end=days[399], horizons=[5, 20])
    # days[50] is inside the 100-bar warm-up and before the window; days[150] and [350] count
    assert calls[0]["by_sym"] == {"AAA": [days[150], days[350]]} and calls[0]["sign"] == 1
    assert calls[1]["by_sym"] == {"AAA": [days[200]]} and calls[1]["sign"] == -1
    assert out["symbols"] == [
        {"symbol": "AAA", "buy": 2, "sell": 1},
        {"symbol": "BBB", "buy": 0, "sell": 0, "error": "foo is not defined", "line": 4, "col": 5},
        {"symbol": "CCC", "buy": 0, "sell": 0, "error": "no daily bars in the window"},
    ]
    assert out["stats"]["buy"] == {"signals": 2}
