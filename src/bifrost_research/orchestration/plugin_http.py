"""Shared HTTP helpers for Plugin enqueue (Market / Flex). D10 BLOCKED.

Note: do not use ``from __future__ import annotations``.
"""

import json
import os
import urllib.error
import urllib.request
from typing import Any, Sequence

from dagster import AssetExecutionContext, MaterializeResult


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def post_json(
    url: str,
    body: dict[str, Any],
    *,
    token_header: str,
    token: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            token_header: token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"HTTP {exc.code} POST {url}: {detail}") from exc


def get_json(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def meta(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"advisory": "D10 BLOCKED"}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif value is None:
            continue
        else:
            out[key] = str(value)[:400]
    return out


def enqueue_market_slots(
    context: AssetExecutionContext,
    slots: Sequence[str],
) -> MaterializeResult:
    base = env(
        "MARKET_DATA_API_URL",
        "http://market-data-api.plugin-market-data.svc.cluster.local:8790",
    ).rstrip("/")
    token = env("MARKET_DATA_WRITE_TOKEN")
    if not token:
        raise RuntimeError("MARKET_DATA_WRITE_TOKEN required to enqueue market slots")

    results: list[dict[str, Any]] = []
    for slot in slots:
        url = f"{base}/market/ingest/enqueue-slot"
        context.log.info("enqueue market slot=%s → %s", slot, url)
        result = post_json(
            url,
            {"slot": slot},
            token_header="X-Market-Data-Write-Token",
            token=token,
        )
        results.append({"slot": slot, **(result if isinstance(result, dict) else {})})
        context.log.info("market slot=%s result=%s", slot, result)
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(f"market enqueue failed slot={slot}: {result}")

    return MaterializeResult(
        metadata=meta(
            {
                "slots": ",".join(slots),
                "enqueued_total": sum(int(r.get("enqueued") or 0) for r in results),
                "ok": True,
            }
        )
    )
