"""TD-143: hypothesis → trade, derived from Trade's plans at read time. Plans are invented."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.repositories import hypothesis_trade_links as links


def _plan(pid: int, status: str, trade_id: int | None, *, kind: str = "hypothesis", ref: str | None = "zzz-thesis") -> dict[str, Any]:
    return {
        "strategy_plan_id": pid,
        "symbol": "ZZZ",
        "structure_label": "Short put",
        "source_kind": kind,
        "source_ref": ref,
        "status": status,
        "trade_id": trade_id,
    }


def test_a_filled_hypothesis_plan_links_and_a_cancelled_one_does_not() -> None:
    got = links.links_from_plans(
        [
            _plan(7, "filled", 901),
            _plan(8, "cancelled", 902),  # not a fill, whatever it carries
            _plan(9, "intended", None),
            _plan(10, "filled", 903, kind="symbol"),  # not written from a hypothesis
            _plan(11, "filled", 904, ref="  "),
            _plan(5, "filled", 900, ref=" zzz-thesis "),
        ]
    )
    assert got == {
        "zzz-thesis": [
            {"trade_id": 900, "strategy_plan_id": 5, "symbol": "ZZZ", "structure_label": "Short put"},
            {"trade_id": 901, "strategy_plan_id": 7, "symbol": "ZZZ", "structure_label": "Short put"},
        ]
    }


def test_the_read_asks_for_filled_plans_of_the_named_env(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, str, Any]] = []

    def fake_get(base: str, path: str, params: Any = None, *, timeout: Any = None) -> Any:
        asked.append((base, path, params))
        return {"items": [_plan(7, "filled", 901)], "count": 1}

    monkeypatch.setattr(links, "get", fake_get)
    reading = links.read_trade_links("dev")
    assert asked == [
        (
            "http://api-account.bifrost-dev.svc.cluster.local:8769",
            "/strategies/plans",
            {"status": "filled", "source_kind": "hypothesis", "limit": 500},
        )
    ]
    assert reading["links"]["zzz-thesis"][0]["trade_id"] == 901
    assert reading["error"] is None and reading["truncated"] is False
    # Cached per env: a second read in the window asks nothing.
    links.read_trade_links("dev")
    assert len(asked) == 1
    with pytest.raises(ValueError):
        links.strategy_base("qa")


def test_a_read_at_the_limit_says_it_may_be_short(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_plan(i, "filled", 1000 + i, ref=f"h{i}") for i in range(links.PLANS_LIMIT)]
    monkeypatch.setattr(links, "get", lambda *a, **k: {"items": rows, "count": len(rows)})
    assert links.read_trade_links("stg")["truncated"] is True


def test_a_failed_read_is_null_not_empty() -> None:
    rows: list[dict[str, Any]] = [{"id": "zzz-thesis"}]
    basis = links.attach_trade_links(rows, "prod")
    assert basis["error"] and "unreachable" in basis["error"]
    assert rows[0]["linked_trade_ids"] is None and rows[0]["linked_trades"] is None


def test_the_hypothesis_read_carries_the_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost_research.api import hypothesis as hyp_api
    from bifrost_research.api.app import create_app
    from bifrost_research.auth.deps import require_owner

    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)

    class _Conn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(hyp_api, "_connect_or_503", lambda: _Conn())
    monkeypatch.setattr(hyp_api, "attach_settlement", lambda conn, rows: None)
    monkeypatch.setattr(hyp_api.repo, "get_hypothesis", lambda conn, hid: {"id": hid, "title": "t"})
    monkeypatch.setattr(
        hyp_api.repo,
        "list_hypotheses",
        lambda conn, **kw: [{"id": "zzz-thesis"}, {"id": "other"}],
    )
    monkeypatch.setattr(
        links, "get", lambda *a, **k: {"items": [_plan(7, "filled", 901), _plan(8, "cancelled", 902)], "count": 2}
    )
    app = create_app()
    app.dependency_overrides[require_owner] = lambda: None
    client = TestClient(app)
    one = client.get("/research/hypothesis/zzz-thesis?trade_env=dev").json()["data"]
    assert one["linked_trade_ids"] == [901]
    assert one["trade_link_basis"]["trade_env"] == "dev"
    listed = client.get("/research/hypothesis?trade_env=dev").json()["data"]
    assert [r["linked_trade_ids"] for r in listed["rows"]] == [[901], []]
    assert listed["trade_link_basis"]["plans_read"] == 2
    assert client.get("/research/hypothesis?trade_env=qa").status_code == 422


def test_the_kind_is_sent_and_still_checked_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """TD-178: trade-api 0.11.0 filters by source_kind; one before it ignores the name and
    answers filled plans of every kind. Both answers give the same links, and a read at
    the cap still says it may be short."""
    asked: list[Any] = []
    mixed = [_plan(7, "filled", 901), _plan(10, "filled", 903, kind="symbol"), _plan(12, "filled", 905, kind="manual")]

    def old_api(base: str, path: str, params: Any = None, *, timeout: Any = None) -> Any:
        asked.append(params)
        return {"items": mixed, "count": len(mixed)}  # ignores source_kind

    monkeypatch.setattr(links, "get", old_api)
    reading = links.read_trade_links("stg", use_cache=False)
    assert asked[-1]["source_kind"] == "hypothesis"
    assert reading["links"] == {
        "zzz-thesis": [{"trade_id": 901, "strategy_plan_id": 7, "symbol": "ZZZ", "structure_label": "Short put"}]
    }
    assert reading["plans_read"] == 3 and reading["truncated"] is False
    assert "source_kind=hypothesis" in reading["source"]

    # The old api's cap is filled by other kinds: no link, and it says so.
    others = [_plan(i, "filled", 2000 + i, kind="manual") for i in range(links.PLANS_LIMIT)]
    monkeypatch.setattr(links, "get", lambda *a, **k: {"items": others, "count": len(others)})
    capped = links.read_trade_links("stg", use_cache=False)
    assert capped["links"] == {} and capped["truncated"] is True
