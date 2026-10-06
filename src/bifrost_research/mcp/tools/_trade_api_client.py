"""Shared HTTP client for Bifrost Trade API (Wave RS-F5).

Read-only client — the Research MCP only ever calls GET on three Trade API
processes (monitor / account / market). No POST / PUT / DELETE path is exposed
here. D10 (live trading) remains blocked at the Trade side; this module cannot
bypass that.

Endpoints resolve via K8s cluster DNS by default, one Service per process
(TD-55, Owner 2026-10-04 option B):

- `api-monitor.bifrost-prod.svc.cluster.local:8765` → `/status`, `/risk_summary`
- `api-account.bifrost-prod.svc.cluster.local:8769` → `/executions`, `/performance`
  (base_trading) and `/trades`, `/gate-sets`, `/strategies/opportunities` (base_strategy)
- `api-market.bifrost-prod.svc.cluster.local:8772`  → `/watchlist`, `/quotes`

`api-trading` and `api-strategy` are alias Services of api-account that Trade
removes in TD-55 B2; Research must not name them.

Overridable via env vars for dev / staging:

- `TRADE_API_MONITOR_URL`
- `TRADE_API_TRADING_URL`
- `TRADE_API_STRATEGY_URL`
- `TRADE_API_MARKET_URL`
- `TRADE_API_TIMEOUT` (seconds, default 8.0)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
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
        "http://api-account.bifrost-prod.svc.cluster.local:8769",
    )


def base_strategy() -> str:
    return _get_env(
        "TRADE_API_STRATEGY_URL",
        "http://api-account.bifrost-prod.svc.cluster.local:8769",
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


def get(
    base: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> Any:
    """Read-only GET. Returns parsed JSON. Raises `RuntimeError` on failure.

    `timeout` overrides `TRADE_API_TIMEOUT` for a single call. Callers that only
    want to enrich a result — as opposed to needing it — should pass something
    short: the default 8s is the right budget for a read the caller depends on,
    and the wrong one for an optional overlay that fails soft.
    """
    url = f"{base}{path if path.startswith('/') else '/' + path}"
    try:
        with httpx.Client(timeout=timeout if timeout is not None else _timeout()) as client:
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


class TradeApiShapeError(RuntimeError):
    """A Trade API answer that does not carry the list the caller reads."""


def list_items(payload: Any, legacy_key: str | None = None) -> list[Mapping[str, Any]]:
    """The rows of a Trade API list answer — the one place Research reads that shape.

    Every Trade API list is ``{items, count, total?, …}`` (``common/envelopes.list_body``;
    ``items`` since api 0.2.3, the only key since api 0.4.0). ``legacy_key`` is the
    route's retired key (``attributions``, ``executions`` …), read only when
    ``items`` is absent. An answer with neither is raised, not read as no rows:
    on 2026-10-06 option_pinned read the retired keys of an api 0.4.0 answer,
    saw zero legs and reported success (TD-89). ``count`` must agree with ``items``.
    """
    if not isinstance(payload, Mapping):
        raise TradeApiShapeError(f"trade api list answer is {type(payload).__name__}, not an object")
    for key in ("items", legacy_key):
        if key is None or key not in payload:
            continue
        rows = payload[key]
        if not isinstance(rows, list):
            raise TradeApiShapeError(f"trade api list key {key!r} is {type(rows).__name__}, not a list")
        count = payload.get("count")
        if key == "items" and isinstance(count, int) and count != len(rows):
            raise TradeApiShapeError(f"trade api list holds {len(rows)} items but count={count}")
        return [r for r in rows if isinstance(r, Mapping)]
    keys = ", ".join(sorted(str(k) for k in payload)) or "none"
    wanted = "items" + (f" or {legacy_key}" if legacy_key else "")
    raise TradeApiShapeError(f"trade api list answer has no {wanted} (keys: {keys})")
