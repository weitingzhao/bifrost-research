"""HTTP client for the pine-runner (``pine-runner/`` in this repo, AGPL, its own process).

Research never imports PineTS: it sends a script and bars over HTTP and gets
back the sessions each ``buy`` / ``sell`` plot fired.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

DEFAULT_URL = "http://research-pine.research.svc.cluster.local:8797"


def runner_url() -> str:
    return (os.environ.get("PINE_RUNNER_URL") or DEFAULT_URL).rstrip("/")


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _day(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def _post(path: str, payload: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        runner_url() + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — in-cluster URL from config
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            body = {}
        raise ValueError(body.get("error") or f"pine-runner HTTP {exc.code}") from exc


def health(timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(runner_url() + "/health", timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def run(
    source: str,
    series: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    timeout: float = 120.0,
) -> dict[str, dict[str, Any]]:
    """Run ``source`` over each symbol's bars ``[{date, open, high, low, close, volume}]``.

    Returns ``{symbol: {"buy": [date], "sell": [date]}}`` or ``{symbol: {"error": str}}``.
    """
    payload = {
        "source": source,
        "series": [
            {
                "symbol": sym,
                "bars": [
                    {
                        "t": _ms(b["date"]),
                        "o": b.get("open") if b.get("open") is not None else b["close"],
                        "h": b.get("high") if b.get("high") is not None else b["close"],
                        "l": b.get("low") if b.get("low") is not None else b["close"],
                        "c": b["close"],
                        "v": b.get("volume") or 0,
                    }
                    for b in bars
                ],
            }
            for sym, bars in series.items()
        ],
    }
    body = _post("/run", payload, timeout)
    out: dict[str, dict[str, Any]] = {}
    for r in body.get("results") or []:
        sym = str(r.get("symbol"))
        if r.get("error"):
            out[sym] = {"error": r["error"]}
        else:
            out[sym] = {"buy": [_day(t) for t in r.get("buy") or []], "sell": [_day(t) for t in r.get("sell") or []]}
    return out
