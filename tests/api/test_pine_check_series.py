"""POST /research/pine/check: a library script by id, and numeric plot series (P1, G10)."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.engines.pine.library import PineScript, builtin_scripts, is_overlay, plot_titles

_MOD = "bifrost_research.api.pine"
_PATH = "/research/pine/check"
_SRC = '//@version=5\nindicator("st")\n[st, dir] = ta.supertrend(3.0, 10)\nplot(st, "Supertrend")\nplotshape(dir < 0, "buy")'


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    monkeypatch.setattr(f"{_MOD}.connect", lambda: MagicMock())
    monkeypatch.setattr(f"{_MOD}.load_bars", lambda conn, sym, start, end: [{"date": d1, "close": 10.0}, {"date": d2, "close": 11.0}])
    lib = {"supertrend": PineScript(id="supertrend", name="Supertrend flip", source=_SRC, version=3, origin="bifrost")}
    monkeypatch.setattr(f"{_MOD}.get_script", lambda conn, sid: lib.get(sid))

    def run(source, series, **kw):  # noqa: ANN001
        seen.append({"source": source, **kw})
        out = {"buy": [d2], "sell": [], "warnings": []}
        if kw.get("plots"):
            out["series"] = {t: ({d2: 9.5, d1: None} if t == "Supertrend" else {}) for t in kw["plots"]}
        return {sym: out for sym in series}

    monkeypatch.setattr(f"{_MOD}.client.run", run)
    return seen


@pytest.fixture
def api() -> TestClient:
    return TestClient(create_app())


def test_a_library_script_by_id_with_its_line(api: TestClient, calls: list[dict[str, Any]]) -> None:
    resp = api.post(_PATH, json={"script": "supertrend", "symbol": "spy", "plots": ["Supertrend"]})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert calls == [{"source": _SRC, "plots": ["Supertrend"]}]
    assert data["series"] == {"Supertrend": [["2026-01-05", None], ["2026-01-06", 9.5]]}  # oldest first
    assert (data["script"], data["script_version"], data["symbol"]) == ("supertrend", 3, "SPY")
    assert data["marks"] == [{"date": "2026-01-06", "side": "buy", "close": 11.0}]


def test_a_pasted_source_answers_as_before(api: TestClient, calls: list[dict[str, Any]]) -> None:
    resp = api.post(_PATH, json={"source": _SRC, "symbol": "SPY"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert calls == [{"source": _SRC, "plots": None}]
    assert "series" not in data and "script" not in data


@pytest.mark.parametrize(
    "body",
    [
        {"symbol": "SPY"},  # neither
        {"source": _SRC, "script": "supertrend", "symbol": "SPY"},  # both
        {"script": "supertrend", "symbol": "SPY", "plots": [""]},
        {"script": "supertrend", "symbol": "SPY", "plots": [f"p{i}" for i in range(9)]},
    ],
)
def test_bad_bodies_are_422(api: TestClient, calls: list[dict[str, Any]], body: dict[str, Any]) -> None:
    assert api.post(_PATH, json=body).status_code == 422
    assert calls == []


def test_an_unknown_script_is_404(api: TestClient, calls: list[dict[str, Any]]) -> None:
    resp = api.post(_PATH, json={"script": "nope", "symbol": "SPY"})
    assert resp.status_code == 404 and calls == []


def test_plot_titles_are_the_numeric_lines_only() -> None:
    src = """plot(hi, "upper")
plot(lo, title="lower")
plot(close > open ? 1 : 0, "buy")
plotshape(x, "sell")
plotchar(y, "char")
plot(hi, "upper")"""
    assert plot_titles(src) == ["upper", "lower"]
    assert plot_titles(_SRC) == ["Supertrend"]
    assert plot_titles("") == []


def test_overlay_marks_the_scripts_whose_lines_are_prices() -> None:
    lines = {s.id: (is_overlay(s.source), plot_titles(s.source)) for s in builtin_scripts()}
    assert lines["supertrend"] == (True, ["Supertrend"])
    assert lines["donchian_breakout"] == (True, ["upper", "lower"])
    assert lines["ichimoku_tk"] == (True, ["tenkan", "kijun"])
    assert lines["chandelier_exit"] == (True, ["Chandelier stop"])
    assert lines["wavetrend"][0] is False and lines["adx_trend"][0] is False
    assert is_overlay('strategy("s", overlay = true, initial_capital=1)')
    assert not is_overlay('indicator("x", overlay=false)')
    row = builtin_scripts()[0].to_dict(with_source=False)
    assert {"plots", "overlay"} <= set(row)


# -- S6: option context ------------------------------------------------------------------

_CTX_SRC = '//@version=5\nindicator("v")\nv = request.security("VRP_20", timeframe.period, close)\nplotshape(v > 0, "buy")'


def test_a_context_script_is_sent_its_series_and_told_when_signals_count(
    api: TestClient, calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    loaded: list[list[str]] = []

    def load(conn, names, bars):  # noqa: ANN001
        loaded.append(list(names))
        return {"SPY": {"VRP_20": {d1: None, d2: 2.0}}}, {}, {"SPY": d2}

    monkeypatch.setattr(f"{_MOD}.context.load", load)
    resp = api.post(_PATH, json={"source": _CTX_SRC, "symbol": "SPY"})
    assert resp.status_code == 200, resp.text
    assert loaded == [["VRP_20"]]
    assert calls == [{"source": _CTX_SRC, "plots": None, "context": {"SPY": {"VRP_20": {d1: None, d2: 2.0}}}, "market": {}}]
    assert resp.json()["data"]["context"] == {"series": ["VRP_20"], "warm_from": "2026-01-06"}


def test_an_unknown_series_or_a_higher_timeframe_is_400(api: TestClient, calls: list[dict[str, Any]]) -> None:
    for src in (_CTX_SRC.replace("VRP_20", "VRP_21"), _CTX_SRC.replace("timeframe.period", '"W"')):
        resp = api.post(_PATH, json={"source": src, "symbol": "SPY"})
        assert resp.status_code == 400
    assert calls == []


def test_the_context_catalog() -> None:
    api = TestClient(create_app())
    resp = api.get("/research/pine/context")
    assert resp.status_code == 200
    data = resp.json()["data"]
    names = [s["name"] for s in data["series"]]
    assert names[:3] == ["IV_30", "IV_RANK", "IV_PCTL"] and len(names) == 12
    iv = data["series"][0]
    assert iv["pine"] == 'request.security("IV_30", timeframe.period, close)' and iv["history_from"] == "2024-09-09"
    assert "5 sessions" in data["rules"]["missing_day"]
