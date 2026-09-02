"""A policy template that saves must be one the runtime honours.

The Loop's strategy lived as a hardcoded constant in two codebases at once, so it
could not be tuned without a release and the two copies were free to drift. Moving
it into the database is only an improvement if the stored shape is validated
against the same model the runtime parses — otherwise the drift just moves.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from bifrost_research.api import policy_template as api
from bifrost_research.copilot.harness.policy_schema import (
    LoopPolicy,
    default_stock_composite_policy,
    validate_policy_for_mode,
)
from bifrost_research.repositories import loop_policy_template as tpl_repo
from bifrost_research.repositories.loop_policy_template import PolicyValidationError

TPL = {
    "id": "lpt-1",
    "name": "stock-first",
    "description": "",
    "universe_mode": "stock_composite",
    "policy_json": {"universe_mode": "stock_composite"},
    "is_default": True,
    "owner_id": "owner",
}


class _Conn:
    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(api, "_connect_or_503", lambda: _Conn())


# ---------------- validation ----------------


def test_the_shipped_default_is_a_valid_template() -> None:
    """The seed has to survive its own validator, or the table starts invalid."""
    normalised, warnings = tpl_repo.validate_policy(default_stock_composite_policy())
    assert normalised["universe_mode"] == "stock_composite"
    assert warnings == []


def test_a_malformed_policy_is_refused_not_stored() -> None:
    with pytest.raises(PolicyValidationError):
        tpl_repo.validate_policy({"universe_mode": "not_a_mode"})


def test_validation_normalises_through_the_runtime_model() -> None:
    """Stored policy is LoopPolicy's dump, so the runtime cannot read it differently."""
    normalised, _ = tpl_repo.validate_policy({"universe_mode": "scan_legacy"})
    assert normalised == LoopPolicy(universe_mode="scan_legacy").model_dump()


def test_a_zero_to_one_min_composite_score_is_called_out() -> None:
    """The frontend template shipped 0.55 against a 0-100 scale — it filters nothing.

    A warning rather than a rejection: an objective may already hold this value,
    and refusing to load it would be worse than naming it.
    """
    warnings = validate_policy_for_mode(
        LoopPolicy(universe_mode="scan_legacy", min_composite_score=0.55)
    )
    assert any("0–1 fraction" in w for w in warnings)
    assert any("filters nothing" in w for w in warnings)


def test_a_real_threshold_draws_no_warning() -> None:
    assert not any(
        "fraction" in w
        for w in validate_policy_for_mode(
            LoopPolicy(universe_mode="scan_legacy", min_composite_score=70.0)
        )
    )


def test_validate_endpoint_reports_400_with_the_parser_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(HTTPException) as e:
        api.validate_policy_body(api.PolicyBody(policy_json={"max_candidates": 999}))
    assert e.value.status_code == 400


def test_validate_endpoint_returns_warnings_without_saving() -> None:
    out = api.validate_policy_body(
        api.PolicyBody(policy_json={"universe_mode": "scan_legacy", "min_composite_score": 0.55})
    )
    assert out["data"]["warnings"], "a 0-1 threshold must be surfaced while editing"


# ---------------- delete guard ----------------


def test_delete_is_refused_while_objectives_name_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """No foreign key defends this link — it lives in objective.policy_json."""
    deleted: list[str] = []
    monkeypatch.setattr(tpl_repo, "get_template", lambda conn, tid: TPL)
    monkeypatch.setattr(tpl_repo, "count_objectives_using", lambda conn, tid: 4)
    monkeypatch.setattr(
        tpl_repo, "delete_template", lambda conn, tid: bool(deleted.append(tid)) or True
    )

    with pytest.raises(HTTPException) as e:
        api.delete_policy_template("lpt-1")

    assert e.value.status_code == 409
    assert "4 objective(s)" in str(e.value.detail)
    assert deleted == [], "refused delete must not reach the repository"


def test_delete_is_allowed_when_nothing_was_built_from_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(tpl_repo, "get_template", lambda conn, tid: TPL)
    monkeypatch.setattr(tpl_repo, "count_objectives_using", lambda conn, tid: 0)
    monkeypatch.setattr(
        tpl_repo, "delete_template", lambda conn, tid: bool(deleted.append(tid)) or True
    )
    out = api.delete_policy_template("lpt-1")
    assert out["data"]["deleted"] is True
    assert deleted == ["lpt-1"]


def test_deleting_an_unknown_template_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tpl_repo, "get_template", lambda conn, tid: None)
    with pytest.raises(HTTPException) as e:
        api.delete_policy_template("nope")
    assert e.value.status_code == 404


# ---------------- lineage query runs for real ----------------


class _CountCursor:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def __enter__(self) -> "_CountCursor":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._store["sql"] = sql
        self._store["params"] = params

    def fetchone(self) -> tuple:
        return (2,)


class _CountConn:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def cursor(self) -> _CountCursor:
        return _CountCursor(self.store)


def test_count_objectives_using_executes_its_query() -> None:
    """Every other test here stubs it; this one runs the SQL.

    A missing table-name import survived a whole suite once because every test
    monkeypatched the function that used it.
    """
    conn = _CountConn()
    assert tpl_repo.count_objectives_using(conn, "lpt-1") == 2
    assert "source_template_id" in conn.store["sql"]
    assert "objective" in conn.store["sql"]
    assert conn.store["params"] == ("lpt-1",)
