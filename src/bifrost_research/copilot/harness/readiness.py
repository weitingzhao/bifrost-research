"""Harness data readiness — Wave LS-2c fail-soft freshness gates."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Protocol

from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness.policy_schema import LoopPolicy

logger = logging.getLogger(__name__)


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def sepa_stale_days(conn: _Connection, *, today: date | None = None) -> int | None:
    sql = "SELECT MAX(trade_date) FROM features.stock_signal_sepa_daily"
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            val = row[0]
            latest = val if isinstance(val, date) else date.fromisoformat(str(val)[:10])
            ref = today or date.today()
            return max(0, (ref - latest).days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sepa_stale_days failed: %s", exc)
        return None


def check_sepa_fresh(conn: _Connection, max_stale_days: int = 3) -> tuple[bool, str]:
    stale = sepa_stale_days(conn)
    if stale is None:
        return False, "no sepa model rows"
    if stale > max_stale_days:
        return False, f"sepa snapshot {stale}d old (max {max_stale_days})"
    return True, f"sepa ok ({stale}d old)"


def check_scan_fresh(conn: _Connection, max_stale_days: int = 3) -> tuple[bool, str]:
    stale = ds.scan_stale_days(conn)
    if stale is None:
        return False, "no scan rows"
    if stale > max_stale_days:
        return False, f"scan snapshot {stale}d old (max {max_stale_days})"
    return True, f"scan ok ({stale}d old)"


def overlay_readiness(
    conn: _Connection,
    policy: LoopPolicy,
    *,
    max_stale_days: int = 3,
) -> tuple[bool, str]:
    """Return whether option overlay can run; fail-soft skip when stale."""
    if not policy.option_overlay.enabled:
        return True, "overlay disabled"
    ok, msg = check_scan_fresh(conn, max_stale_days)
    if not ok:
        return False, f"overlay skipped: {msg}"
    return True, msg
