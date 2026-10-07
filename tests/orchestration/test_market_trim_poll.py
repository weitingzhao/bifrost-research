"""TD-106: market_trim polls the plugin job instead of treating 202 as finished.

The timeout is the same number the plugin exports
(trim_flight.TRIM_CLIENT_TIMEOUT_SEC). This CI cannot import the plugin.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("dagster")

from bifrost_research.orchestration import plugin_http
from bifrost_research.orchestration.plugin_http import (
    TRIM_CLIENT_TIMEOUT_SEC,
    enqueue_market_trim,
)


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, msg: str, *args: Any) -> None:
        self.lines.append(msg % args if args else msg)

    info = warning = error = debug = _record


class _Context:
    def __init__(self) -> None:
        self.log = _Log()


def test_timeout_matches_the_plugin_constant() -> None:
    assert TRIM_CLIENT_TIMEOUT_SEC == 1200.0
    text = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "src/bifrost_research/orchestration/market_slot_schedules.py"
    ).read_text()
    assert "single_flight=True" in text
    assert "enqueue_market_trim" in text


def test_already_running_polls_the_same_job(monkeypatch: pytest.MonkeyPatch) -> None:
    polls: list[str] = []

    def _post(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"ok": True, "status": "already_running", "job_id": "44", "kind": "slot-trim"}

    def _get(url: str, **_k: Any) -> dict[str, Any]:
        polls.append(url)
        status = "running" if len(polls) == 1 else "done"
        return {
            "ok": True,
            "job": {
                "id": 44,
                "status": status,
                "result": {"retention_archive": {"raw_market.option_daily": {"rows": 3}}},
            },
        }

    monkeypatch.setattr(plugin_http, "post_json", _post)
    monkeypatch.setattr(plugin_http, "get_json", _get)
    monkeypatch.setenv("MARKET_DATA_WRITE_TOKEN", "t")
    out = enqueue_market_trim(_Context(), sleep=lambda _s: None, now=lambda: 0.0)  # type: ignore[arg-type]
    assert polls == [
        "http://market-data-api.plugin-market-data.svc.cluster.local:8790/market/ingest/jobs/44",
        "http://market-data-api.plugin-market-data.svc.cluster.local:8790/market/ingest/jobs/44",
    ]
    assert out.metadata["job_id"] == "44"
    assert out.metadata["retention_archive"] == "yes"


def test_a_failed_trim_fails_the_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        plugin_http,
        "post_json",
        lambda *_a, **_k: {"ok": True, "status": "accepted", "job_id": "7"},
    )
    monkeypatch.setattr(
        plugin_http,
        "get_json",
        lambda *_a, **_k: {"ok": True, "job": {"status": "failed", "result": {"error": "boom"}}},
    )
    monkeypatch.setenv("MARKET_DATA_WRITE_TOKEN", "t")
    with pytest.raises(RuntimeError, match="boom"):
        enqueue_market_trim(_Context(), sleep=lambda _s: None, now=lambda: 0.0)  # type: ignore[arg-type]


def test_poll_stops_at_the_client_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}

    def _now() -> float:
        clock["t"] += TRIM_CLIENT_TIMEOUT_SEC
        return clock["t"]

    monkeypatch.setattr(
        plugin_http,
        "post_json",
        lambda *_a, **_k: {"ok": True, "status": "accepted", "job_id": "8"},
    )
    monkeypatch.setattr(
        plugin_http,
        "get_json",
        lambda *_a, **_k: {"ok": True, "job": {"status": "running"}},
    )
    monkeypatch.setenv("MARKET_DATA_WRITE_TOKEN", "t")
    with pytest.raises(RuntimeError, match="still running"):
        enqueue_market_trim(_Context(), sleep=lambda _s: None, now=_now)  # type: ignore[arg-type]
