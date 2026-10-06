"""Suite-wide guards."""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_research.repositories import hypothesis_trade_links


@pytest.fixture(autouse=True)
def _no_trade_api_from_hypothesis_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hypothesis read overlays Trade's plans (TD-143). No test reaches a real
    Trade API: by default the read fails as an unreachable one would, and a test
    that wants plans patches ``hypothesis_trade_links.get`` itself."""

    def unreachable(base: str, path: str, params: Any = None, *, timeout: Any = None) -> Any:
        raise RuntimeError(f"trade api GET {base}{path} unreachable: no network in tests")

    monkeypatch.setattr(hypothesis_trade_links, "get", unreachable)
    hypothesis_trade_links._cache.clear()
