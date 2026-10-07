"""What the Flex plugin's freshness KPIs say about today's ingest.

Pure so the gate's rule is testable without Dagster: the plugin's
``/flex/dashboard/freshness-kpis`` payload in, one verdict out. No Dagster import
here: ``flex_ingest_now`` also gates a reader outside the batch (the journal
distill, which research-api runs too).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Saturday 06:30 ET collects Friday; by Monday 22:30 ET that success is ~64h old.
DEFAULT_MAX_AGE_HOURS = 96.0
FLEX_QUERY_API_DEFAULT = "http://flex-query-api.plugin-flex-query.svc.cluster.local:8791"
#: Seconds before the one retry of a freshness probe that raised.
NOW_RETRY_SEC = 15.0


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def flex_ingest_verdict(
    kpis: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> tuple[str, str]:
    """(verdict, reason). Verdicts: ``ok`` | ``failed`` | ``stale`` | ``unknown``.

    ``failed`` — the newest attempt of some kind did not succeed (after the
    morning's retry budget that is a real failure, not a statement still
    generating). ``stale`` — a kind has not succeeded within ``max_age_hours``.
    """
    now = now or datetime.now(UTC)
    dims = list((kpis or {}).get("dimensions") or [])
    if not dims:
        return "unknown", "freshness-kpis carries no dimensions"
    failed: list[str] = []
    stale: list[str] = []
    for d in dims:
        kind = str(d.get("kind") or "?")
        if d.get("last_ok") is False:
            failed.append(f"{kind}: {str(d.get('last_error') or 'last attempt failed')[:160]}")
            continue
        last = _parse_iso(d.get("last_success_at"))
        if last is None:
            stale.append(f"{kind}: never succeeded")
        elif (now - last).total_seconds() > max_age_hours * 3600:
            stale.append(f"{kind}: last success {last.isoformat()} older than {max_age_hours:g}h")
    if failed:
        return "failed", "; ".join(failed)
    if stale:
        return "stale", "; ".join(stale)
    return "ok", f"{len(dims)} kinds fresh"


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30.0) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def flex_ingest_now(
    *,
    get: Callable[[str], Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """batch/flex_gate's Flex verdict, for a Flex reader outside the batch (TD-244).

    Same probe (``/flex/dashboard/freshness-kpis``), same rule
    (``flex_ingest_verdict``), same knobs (``FLEX_QUERY_API_URL``,
    ``FLEX_GATE_MAX_AGE_HOURS``). Never raises: a probe that does not answer after
    one retry is ``unknown``, which a caller treats as not fresh.
    """
    base = (os.environ.get("FLEX_QUERY_API_URL") or FLEX_QUERY_API_DEFAULT).strip().rstrip("/")
    max_age = float(os.environ.get("FLEX_GATE_MAX_AGE_HOURS") or DEFAULT_MAX_AGE_HOURS)
    fetch = get or _get_json
    error = "unreachable"
    for attempt in (1, 2):
        try:
            kpis = fetch(f"{base}/flex/dashboard/freshness-kpis")
        except Exception as exc:  # noqa: BLE001 — unknown, the caller skips the read
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("flex freshness probe failed (attempt %d): %s", attempt, error)
            if attempt == 1:
                time.sleep(NOW_RETRY_SEC)
            continue
        if not isinstance(kpis, Mapping):
            return "unknown", "freshness-kpis is not a JSON object"
        return flex_ingest_verdict(kpis, now=now, max_age_hours=max_age)
    return "unknown", f"freshness-kpis probe failed: {error}"
