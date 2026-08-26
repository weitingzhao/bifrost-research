"""Shared HTTP client for Bifrost Trade API (Wave RS-F5).

Read-only client — the Research MCP only ever calls GET on the four Trade API
domains (monitor / trading / strategy / market). No POST / PUT / DELETE path
is exposed here. D10 (live trading) remains blocked at the Trade side; this
module cannot bypass that.

Endpoints resolve via K8s cluster DNS by default:

- `api-monitor.bifrost-prod.svc.cluster.local:8765`  → `/status`, `/risk_summary`
- `api-trading.bifrost-prod.svc.cluster.local:8769`  → `/executions`
- `api-strategy.bifrost-prod.svc.cluster.local:8769` → `/instances`, `/opportunities`
- `api-market.bifrost-prod.svc.cluster.local:8772`   → `/watchlist`, `/quotes`

Overridable via env vars for dev / staging:

- `TRADE_API_MONITOR_URL`
- `TRADE_API_TRADING_URL`
- `TRADE_API_STRATEGY_URL`
- `TRADE_API_MARKET_URL`
- `TRADE_API_TIMEOUT` (seconds, default 8.0)
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_TIMEOUT = 8.0


def _get_env(name: str, default: str) -> str:
    return (os.environ.get(name) or default).rstrip("/")


def base_monitor() -> str:
    return _get_env(
        "TRADE_API_MONITOR_URL",
        "http://api-monitor.bifrost-prod.svc.cluster.local:8765",
    )


def base_trading() -> str:
    return _get_env(
        "TRADE_API_TRADING_URL",
        "http://api-trading.bifrost-prod.svc.cluster.local:8769",
    )


def base_strategy() -> str:
    return _get_env(
        "TRADE_API_STRATEGY_URL",
        "http://api-strategy.bifrost-prod.svc.cluster.local:8769",
    )


def base_market() -> str:
    return _get_env(
        "TRADE_API_MARKET_URL",
        "http://api-market.bifrost-prod.svc.cluster.local:8772",
    )


def _timeout() -> float:
    try:
        return float(os.environ.get("TRADE_API_TIMEOUT") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def get(base: str, path: str, params: dict[str, Any] | None = None) -> Any:
    """Read-only GET. Returns parsed JSON. Raises `RuntimeError` on failure."""
    url = f"{base}{path if path.startswith('/') else '/' + path}"
    try:
        with httpx.Client(timeout=_timeout()) as client:
            resp = client.get(url, params=params or {})
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        body_snip = (exc.response.text or "")[:200]
        raise RuntimeError(
            f"trade api GET {url} → HTTP {exc.response.status_code}: {body_snip}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"trade api GET {url} unreachable: {exc}") from exc
    except ValueError as exc:  # json decode
        raise RuntimeError(f"trade api GET {url} returned non-JSON: {exc}") from exc
