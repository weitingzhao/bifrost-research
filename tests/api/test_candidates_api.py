"""Offline unit tests for candidate_pool helpers (no DB)."""

from __future__ import annotations

import re

import pytest

from bifrost_research.repositories.candidate_pool import (
    _ALLOWED_SOURCES,
    _ALLOWED_STATUSES,
    generate_candidate_id,
)


def test_generate_candidate_id_format() -> None:
    cid = generate_candidate_id("AAPL")
    assert cid.startswith("cand-aapl-")
    # cand-{sym}-{6 hex ts}{4 hex token}
    assert re.fullmatch(r"cand-aapl-[0-9a-f]{6}[0-9a-f]{4}", cid)


def test_generate_candidate_id_empty_symbol_fallback() -> None:
    cid = generate_candidate_id("")
    assert cid.startswith("cand-sym-")
    assert re.fullmatch(r"cand-sym-[0-9a-f]{10}", cid)


def test_generate_candidate_id_uppercases_and_truncates() -> None:
    cid = generate_candidate_id("  verylongsymbolname  ")
    assert cid.startswith("cand-verylongsymb-")


def test_allowed_sources_contains_loop_sources() -> None:
    for src in ("scan", "manual", "harness", "copilot", "sepa"):
        assert src in _ALLOWED_SOURCES
    assert "invalid_source" not in _ALLOWED_SOURCES


def test_allowed_statuses() -> None:
    assert _ALLOWED_STATUSES == frozenset({"open", "promoted", "dismissed", "expired"})


def test_create_candidate_rejects_invalid_source_without_db() -> None:
    """Validation runs before any DB call — pass a dummy conn that must not be used."""

    class _Boom:
        def cursor(self) -> None:  # pragma: no cover
            raise AssertionError("DB should not be touched on invalid source")

        def commit(self) -> None:  # pragma: no cover
            raise AssertionError("DB should not be touched on invalid source")

    from bifrost_research.repositories.candidate_pool import create_candidate

    with pytest.raises(ValueError, match="invalid source"):
        create_candidate(_Boom(), symbol="AAPL", source="not-a-source")  # type: ignore[arg-type]


def test_list_candidates_rejects_invalid_status_without_db() -> None:
    class _Boom:
        def cursor(self) -> None:  # pragma: no cover
            raise AssertionError("DB should not be touched on invalid status")

        def commit(self) -> None:  # pragma: no cover
            raise AssertionError("DB should not be touched on invalid status")

    from bifrost_research.repositories.candidate_pool import list_candidates

    with pytest.raises(ValueError, match="invalid status"):
        list_candidates(_Boom(), status="bogus")  # type: ignore[arg-type]
