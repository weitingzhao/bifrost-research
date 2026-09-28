"""husbandry_gate: the Market side is judged on a freshly computed doctor report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("dagster")

from dagster import build_asset_context

from bifrost_research.orchestration import plugin_batch_assets as pba

GEN = "2026-09-05T02:30:21+00:00"


def _flex_ok() -> dict[str, Any]:
    """The gate ages Flex against the wall clock, so the fixture must be recent."""
    recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"dimensions": [{"kind": "flex-trades", "last_ok": True, "last_success_at": recent}]}


def _md(result: Any, key: str) -> Any:
    v = result.metadata[key]
    return getattr(v, "value", v)


def _run(monkeypatch: pytest.MonkeyPatch, doctor: dict[str, Any]) -> tuple[Any, list[str]]:
    urls: list[str] = []

    def fake_get(url: str, **kw: Any) -> dict[str, Any]:
        urls.append(url)
        if "/market/doctor" in url:
            return doctor
        if url.endswith("/flex/config/summary"):
            return {"source": "secret"}
        return _flex_ok()

    monkeypatch.setattr(pba, "get_json", fake_get)
    return pba.husbandry_gate(build_asset_context()), urls


def test_the_doctor_read_is_recomputed_and_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain GET /market/doctor is the Plugin's cache — an earlier session, or nothing."""
    result, urls = _run(
        monkeypatch,
        {
            "session": "2026-09-04",
            "generated_at": GEN,
            "verdict": "degraded",
            "eod_critical": {"verdict": "healthy", "detail": ""},
        },
    )
    doctor = [u for u in urls if "/market/doctor" in u]
    assert len(doctor) == 1 and doctor[0].endswith("/market/doctor?probes=false&refresh=true")
    assert _md(result, "market_session") == "2026-09-04"
    assert _md(result, "market_generated_at") == GEN
    assert _md(result, "gate") == "pass"


def test_a_critical_eod_session_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="session 2026-09-04 incomplete"):
        _run(
            monkeypatch,
            {
                "session": "2026-09-04",
                "generated_at": GEN,
                "verdict": "critical",
                "eod_critical": {"verdict": "critical", "detail": "open interest 0/25"},
            },
        )
