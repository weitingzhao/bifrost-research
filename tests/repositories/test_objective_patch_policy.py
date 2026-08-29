"""Wave Y.3 — objective.patch_policy_json whitelist + jsonb merge SQL."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from bifrost_research.repositories import objective as obj_repo


def _mock_conn(fetch_row: tuple[Any, ...] | None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetch_row
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn._cur = cur  # expose for assertions
    return conn


def _sample_obj_row(policy_json: dict[str, Any]) -> tuple[Any, ...]:
    # Matches _OBJ_COLS ordering
    return (
        "obj-abc",
        "Test",
        "Description",
        "adhoc",
        json.dumps(policy_json),
        "loop_curator",
        "active",
        "owner",
        None,
    )


class TestPatchPolicyJson:
    def test_whitelist_filters_unknown_keys(self) -> None:
        conn = _mock_conn(_sample_obj_row({"min_hit_rate": 0.7}))
        result = obj_repo.patch_policy_json(
            conn,
            "obj-abc",
            {
                "min_hit_rate": 0.7,
                "arbitrary_field": "should_be_dropped",
                "seed_symbols": ["AAPL"],  # explicitly not in whitelist
            },
        )
        assert result is not None
        assert result["policy_json"] == {"min_hit_rate": 0.7}
        # SQL received only whitelist key
        called_args = conn._cur.execute.call_args
        params = called_args[0][1]
        merged = json.loads(params[0])
        assert merged == {"min_hit_rate": 0.7}

    def test_empty_patch_returns_current_objective(self) -> None:
        """No whitelist keys → no UPDATE executed, uses get_objective SELECT."""
        conn = _mock_conn(_sample_obj_row({"existing": True}))
        result = obj_repo.patch_policy_json(
            conn, "obj-abc", {"arbitrary_field": "x"}
        )
        assert result is not None
        # Should have run a SELECT via get_objective, not the UPDATE
        sql = conn._cur.execute.call_args[0][0]
        assert "UPDATE" not in sql.upper()
        assert "SELECT" in sql.upper()

    def test_multiple_whitelist_keys_merged(self) -> None:
        conn = _mock_conn(
            _sample_obj_row({"min_hit_rate": 0.65, "preset": "momentum", "keep_me": True})
        )
        result = obj_repo.patch_policy_json(
            conn,
            "obj-abc",
            {"min_hit_rate": 0.65, "preset": "momentum", "not_allowed": 1},
        )
        assert result is not None
        params = conn._cur.execute.call_args[0][1]
        merged = json.loads(params[0])
        assert merged == {"min_hit_rate": 0.65, "preset": "momentum"}
        # SQL uses jsonb `||` operator
        sql = conn._cur.execute.call_args[0][0]
        assert "||" in sql

    def test_none_whitelist_disables_filtering(self) -> None:
        conn = _mock_conn(_sample_obj_row({"anything": True}))
        obj_repo.patch_policy_json(
            conn, "obj-abc", {"anything": True}, whitelist=None
        )
        params = conn._cur.execute.call_args[0][1]
        merged = json.loads(params[0])
        assert merged == {"anything": True}

    def test_non_mapping_raises(self) -> None:
        conn = _mock_conn(None)
        with pytest.raises(TypeError, match="mapping"):
            obj_repo.patch_policy_json(conn, "obj-abc", [1, 2, 3])  # type: ignore[arg-type]

    def test_returns_none_when_objective_missing(self) -> None:
        conn = _mock_conn(None)  # UPDATE returns no row
        result = obj_repo.patch_policy_json(
            conn, "obj-nope", {"min_hit_rate": 0.6}
        )
        assert result is None
