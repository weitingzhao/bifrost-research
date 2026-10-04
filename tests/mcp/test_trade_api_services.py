"""Research names one Trade API Service per process (TD-55, Owner 2026-10-04 option B).

api-trading, api-strategy, api-portfolio, api-ops and api-docs are alias Services of
api-account / api-monitor that Trade removes in B2, after Research stops naming them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bifrost_research.mcp.tools import _trade_api_client as client

ROOT = Path(__file__).resolve().parents[2]
ALIAS_SERVICE = re.compile(r"\bapi-(trading|strategy|portfolio|ops|docs)\.bifrost-")


@pytest.mark.parametrize(
    ("fn", "env", "want"),
    [
        (client.base_monitor, "TRADE_API_MONITOR_URL", "http://api-monitor.bifrost-prod.svc.cluster.local:8765"),
        (client.base_trading, "TRADE_API_TRADING_URL", "http://api-account.bifrost-prod.svc.cluster.local:8769"),
        (client.base_strategy, "TRADE_API_STRATEGY_URL", "http://api-account.bifrost-prod.svc.cluster.local:8769"),
        (client.base_market, "TRADE_API_MARKET_URL", "http://api-market.bifrost-prod.svc.cluster.local:8772"),
    ],
)
def test_default_base_is_the_process_service(monkeypatch, fn, env, want):
    monkeypatch.delenv(env, raising=False)
    assert fn() == want


def test_env_still_overrides(monkeypatch):
    monkeypatch.setenv("TRADE_API_TRADING_URL", "http://example.test:1/")
    assert client.base_trading() == "http://example.test:1"


def test_no_manifest_names_an_alias_service():
    hits = []
    for path in sorted((ROOT / "k8s").rglob("*.yaml")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if ALIAS_SERVICE.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    src = (ROOT / "src" / "bifrost_research" / "mcp" / "tools" / "_trade_api_client.py").read_text()
    for n, line in enumerate(src.splitlines(), 1):
        if ALIAS_SERVICE.search(line):
            hits.append(f"_trade_api_client.py:{n}: {line.strip()}")
    assert hits == []
