"""Tier stats / tier filter / momentum grades / criteria-stats distributions (0.157.0, TD-49 step 2).

Trade API read these straight from Golden Source; Research now serves them with
the shapes Trade API returned, so its routes become a proxy. No database: the
connection is faked and every symbol, date and count below is invented.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator, List

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api import research_engines, sepa_reader, tier_reader
from bifrost_research.api.app import create_app


class _Cur:
    def __init__(self, sink: List[Any], results: List[Any]) -> None:
        self.sink, self.results = sink, results

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.sink.append((" ".join(sql.split()), params))

    def fetchall(self) -> Any:
        return self.results.pop(0)

    def fetchone(self) -> Any:
        return self.results.pop(0)


class _Conn:
    def __init__(self, sink: List[Any], results: List[Any]) -> None:
        self.sink, self.results = sink, results
        self.closed = False

    def cursor(self, **_kw: Any) -> _Cur:
        return _Cur(self.sink, self.results)

    def close(self) -> None:
        self.closed = True


def _fake_get_conn(monkeypatch: pytest.MonkeyPatch, module: Any, results: List[Any]) -> List[Any]:
    sink: List[Any] = []

    @contextmanager
    def _get_conn() -> Iterator[_Conn]:
        yield _Conn(sink, results)

    monkeypatch.setattr(module, "get_conn", _get_conn)
    return sink


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def test_the_routes_are_registered(client: TestClient) -> None:
    paths = set(client.app.openapi()["paths"])
    assert {"/analytics/sepa/tier-stats", "/analytics/sepa/tier-filter", "/research/momentum/grades"} <= paths


def test_the_vocabulary_is_the_marts_columns() -> None:
    assert tier_reader.TIER_MAX_SCORE == {"momentum": 10, "structure": 8, "sentiment": 6}
    assert "bb_squeeze" in tier_reader.TIER_COLUMNS["structure"]


# ── tier-stats ──


def test_tier_stats_counts_every_signal_and_buckets_every_score(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cols = tier_reader.TIER_COLUMNS["sentiment"]
    head = {"d": date(2031, 1, 2), "n": 7, **{c: i for i, c in enumerate(cols)}}
    sink = _fake_get_conn(monkeypatch, tier_reader, [head, [{"s": 0, "c": 3}, {"s": 6, "c": 4}]])
    resp = client.get("/analytics/sepa/tier-stats?tier=sentiment")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ok": True,
        "tier": "sentiment",
        "eval_date": "2031-01-02",
        "universe_count": 7,
        "max_score": 6,
        "conditions": [{"id": c, "pass": i} for i, c in enumerate(cols)],
        "pass_count_distribution": {"0": 3, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 4},
        "signals": list(cols),
    }
    assert "FROM dw_stock.mart_sepa_tier_sentiment WHERE eval_date = (SELECT max(eval_date)" in sink[0][0]
    assert "round(sentiment_score * 6)::int AS s" in sink[1][0]


def test_tier_stats_on_an_empty_mart(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_get_conn(monkeypatch, tier_reader, [{"d": None, "n": 0}, []])
    body = client.get("/analytics/sepa/tier-stats?tier=momentum").json()
    assert body["eval_date"] is None and body["universe_count"] == 0
    assert set(body["pass_count_distribution"].values()) == {0} and len(body["pass_count_distribution"]) == 11


def test_tier_stats_refuses_an_unknown_tier(client: TestClient) -> None:
    resp = client.get("/analytics/sepa/tier-stats?tier=options")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "tier must be one of: ['momentum', 'structure', 'sentiment']"


def test_tier_stats_db_failure_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def _down() -> Iterator[Any]:
        raise RuntimeError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(tier_reader, "get_conn", _down)
    resp = client.get("/analytics/sepa/tier-stats?tier=structure")
    assert resp.status_code == 503 and "connection refused" in resp.json()["detail"]


# ── tier-filter ──


def test_any_of_two_signals_counts_the_whole_match(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"symbol": "ZZA", "score": 6, "eval_date": date(2031, 1, 2), "total": 3}]
    sink = _fake_get_conn(monkeypatch, tier_reader, [rows])
    resp = client.get("/analytics/sepa/tier-filter?tier=structure&include=bb_squeeze,vol_contracting&match=any&limit=1")
    assert resp.status_code == 200
    sql, params = sink[0]
    assert "(bb_squeeze IS TRUE OR vol_contracting IS TRUE)" in sql and "count(*) OVER () AS total" in sql
    assert params == [1]
    assert resp.json() == {
        "ok": True,
        "tier": "structure",
        "include": ["bb_squeeze", "vol_contracting"],
        "match": "any",
        "min_score": 0,
        "max_score": 8,
        "count": 3,
        "truncated": True,
        "eval_date": "2031-01-02",
        "symbols": [{"symbol": "ZZA", "score": 6}],
        "limit": 1,
    }


def test_min_score_is_signals_passed_and_clamped(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _fake_get_conn(monkeypatch, tier_reader, [[]])
    body = client.get("/analytics/sepa/tier-filter?tier=momentum&min_score=99&limit=0").json()
    sql, params = sink[0]
    assert "round(momentum_score * 10)::int >= %s" in sql and params == [10, 1]
    assert body["count"] == 0 and body["truncated"] is False and body["eval_date"] is None and body["min_score"] == 10


def test_all_is_the_default_match(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _fake_get_conn(monkeypatch, tier_reader, [[]])
    client.get("/analytics/sepa/tier-filter?tier=momentum&include=macd_bullish,rs_gt_spy&match=bogus")
    assert "(macd_bullish IS TRUE AND rs_gt_spy IS TRUE)" in sink[0][0]


def test_nothing_picked_is_an_empty_answer_without_a_query(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _fake_get_conn(monkeypatch, tier_reader, [])
    body = client.get("/analytics/sepa/tier-filter?tier=sentiment").json()
    assert body == {"ok": True, "tier": "sentiment", "include": [], "count": 0, "symbols": [], "limit": 500}
    assert sink == []


def test_an_unknown_signal_is_400(client: TestClient) -> None:
    resp = client.get("/analytics/sepa/tier-filter?tier=structure&include=bb_squeeze,vcp_contraction_3m")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "unknown structure signal ids: vcp_contraction_3m"


def test_the_reader_refuses_ids_outside_the_tier() -> None:
    with pytest.raises(ValueError, match="unknown momentum signal ids: bb_squeeze"):
        tier_reader.fetch_tier_filter("momentum", ["bb_squeeze"], 0, "all", 10)


# ── momentum grades ──


def _fake_connect(monkeypatch: pytest.MonkeyPatch, results: List[Any]) -> List[Any]:
    sink: List[Any] = []
    monkeypatch.setattr(research_engines, "connect", lambda: _Conn(sink, results))
    return sink


def test_grades_count_the_latest_session_and_list_the_picked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    counts = [(date(2031, 1, 2), "A", 3), (date(2031, 1, 2), "B", 5), (date(2031, 1, 2), None, 9)]
    sink = _fake_connect(monkeypatch, [counts, [("ZZA",), ("ZZB",)]])
    resp = client.get("/research/momentum/grades?grades=a, b ,Q&limit=2")
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "trade_date": "2031-01-02",
        "counts": {"A+": 0, "A": 3, "B": 5, "C": 0, "D": 0},
        "graded": 8,
        "grades": ["A", "B"],
        "count": 8,
        "truncated": True,
        "symbols": ["ZZA", "ZZB"],
    }
    assert "trade_date = (SELECT max(trade_date) FROM features.stock_signal_momentum_daily)" in sink[0][0]
    assert sink[1][1] == (["A", "B"], 2)


def test_grades_without_a_pick_only_count(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _fake_connect(monkeypatch, [[(date(2031, 1, 2), "C", 4)]])
    body = client.get("/research/momentum/grades").json()
    assert body["grades"] == [] and body["count"] == 0 and body["truncated"] is False and body["symbols"] == []
    assert body["graded"] == 4 and len(sink) == 1


def test_grades_on_an_empty_table(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_connect(monkeypatch, [[], []])
    body = client.get("/research/momentum/grades?grades=A").json()
    assert body["trade_date"] is None and body["graded"] == 0 and body["truncated"] is False


def test_grades_db_failure_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _down() -> Any:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(research_engines, "connect", _down)
    assert client.get("/research/momentum/grades").status_code == 503


# ── criteria-stats pass-count distributions ──


def test_criteria_stats_adds_both_distributions(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stats = [{"domain": "fundamental", "stats": {"evaluated": 9}}, {"domain": "technical", "stats": {"evaluated": 8}}]
    dist_sink: List[Any] = []
    results = [
        {"d": date(2031, 1, 2)},
        [{"conditions_passed": 8, "symbol_count": 2}, {"conditions_passed": 0, "symbol_count": 5}],
        {"d": date(2031, 1, 3)},
        [{"conditions_passed": 11, "symbol_count": 1}],
    ]
    calls = {"n": 0}

    @contextmanager
    def _get_conn() -> Iterator[_Conn]:
        calls["n"] += 1
        # First connection: the stats read; second: the distributions.
        yield _Conn([], [stats]) if calls["n"] == 1 else _Conn(dist_sink, results)

    monkeypatch.setattr(sepa_reader, "get_conn", _get_conn)
    resp = client.get("/analytics/sepa/criteria-stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fundamental"] == {"evaluated": 9} and body["technical"] == {"evaluated": 8}
    assert body["fundamental_eval_date"] == "2031-01-02" and body["technical_eval_date"] == "2031-01-03"
    fund = body["fundamental_distribution"]
    assert [b["conditions_passed"] for b in fund] == list(range(8, -1, -1))
    assert fund[0] == {"conditions_passed": 8, "symbol_count": 2} and fund[-1] == {"conditions_passed": 0, "symbol_count": 5}
    assert sum(b["symbol_count"] for b in fund) == 7
    tech = body["technical_distribution"]
    assert [b["conditions_passed"] for b in tech] == list(range(11, -1, -1)) and tech[0]["symbol_count"] == 1
    fund_sql = dist_sink[1][0]
    tech_sql = dist_sink[3][0]
    assert "COALESCE(insufficient_data, false) IS NOT TRUE" in fund_sql
    assert "insufficient_data" not in tech_sql
    assert dist_sink[1][1] == (date(2031, 1, 2),)


def test_criteria_stats_distributions_on_empty_marts(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_get_conn(monkeypatch, sepa_reader, [{"d": None}, {"d": None}])
    assert sepa_reader.fetch_pass_count_distributions() == {
        "fundamental_distribution": [],
        "fundamental_eval_date": None,
        "technical_distribution": [],
        "technical_eval_date": None,
    }
