"""What the Flex plugin's freshness KPIs say about today's ingest.

Pure so the gate's rule is testable without Dagster: the plugin's
``/flex/dashboard/freshness-kpis`` payload in, one verdict out.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

# Saturday 06:30 ET collects Friday; by Monday 22:30 ET that success is ~64h old.
DEFAULT_MAX_AGE_HOURS = 96.0


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
