"""HTTP client for the pine-runner (``pine-runner/`` in this repo, AGPL, its own process).

Research never imports PineTS: it sends a script and bars over HTTP and gets
back the sessions each ``buy`` / ``sell`` plot fired — and, from runner 0.2.0 on
request, numeric plot series and a ``strategy()``'s trades.
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


def _trade(t: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "direction": t.get("direction"),
        "qty": t.get("qty"),
        "entry_date": _day(t["entry_time"]),
        "entry_price": t.get("entry_price"),
        "entry_id": t.get("entry_id"),
        "entry_comment": t.get("entry_comment"),
        "entry_at_open": bool(t.get("entry_at_open")),
    }
    if t.get("exit_time") is not None:
        out.update(
            exit_date=_day(t["exit_time"]),
            exit_price=t.get("exit_price"),
            exit_id=t.get("exit_id"),
            exit_comment=t.get("exit_comment"),
            exit_at_open=bool(t.get("exit_at_open")),
            profit=t.get("profit"),
        )
    return out


def run(
    source: str,
    series: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    timeout: float = 120.0,
    plots: Sequence[str] | None = None,
    trades: bool = False,
) -> dict[str, dict[str, Any]]:
    """Run ``source`` over each symbol's bars ``[{date, open, high, low, close, volume}]``.

    Returns ``{symbol: {"buy": [date], "sell": [date], "warnings": [...]}}`` or
    ``{symbol: {"error": str}}``. ``warnings`` (runner 0.1.1+) flags a script whose
    ``buy`` / ``sell`` title is repeated (merged) or missing; older runners send none.

    Runner 0.2.0, on request (additive keys, absent otherwise):

    - ``plots``: ``"series": {title: {date: float | None}}`` — each numeric plot's
      value per session (None in the warm-up, or where the plot is ``na``).
    - ``trades``: ``"trades": {"closed": [...], "open": [...]}`` for a
      ``strategy()``, ``None`` for an ``indicator()``. Each trade carries the
      sessions its fills fell on (``entry_date`` / ``exit_date``), prices, ids,
      comments, and ``*_at_open``: True when the fill was at the session's open
      (decided on the previous close), False when it filled inside the session.
    """
    payload: dict[str, Any] = {
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
    if plots:
        payload["plots"] = list(plots)
    if trades:
        payload["trades"] = True
    body = _post("/run", payload, timeout)
    out: dict[str, dict[str, Any]] = {}
    for r in body.get("results") or []:
        sym = str(r.get("symbol"))
        if r.get("error"):
            out[sym] = {"error": r["error"]}
            continue
        row: dict[str, Any] = {
            "buy": [_day(t) for t in r.get("buy") or []],
            "sell": [_day(t) for t in r.get("sell") or []],
            "warnings": list(r.get("warnings") or []),
        }
        if plots:
            got = r.get("series")
            if got is None:
                raise ValueError("pine-runner returned no plot series: it predates 0.2.0")
            row["series"] = {title: {_day(t): v for t, v in got.get(title) or []} for title in plots}
        if trades:
            if "trades" not in r:
                raise ValueError("pine-runner returned no trades: it predates 0.2.0")
            tr = r["trades"]
            row["trades"] = (
                None
                if tr is None
                else {"closed": [_trade(t) for t in tr.get("closed") or []], "open": [_trade(t) for t in tr.get("open") or []]}
            )
        out[sym] = row
    return out
