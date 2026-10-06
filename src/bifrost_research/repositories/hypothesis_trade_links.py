"""Hypothesis → trade, derived at read time from Trade's plans (TD-143).

A plan written from a hypothesis carries ``source_kind = 'hypothesis'`` and the
hypothesis id in ``source_ref``; once its intent is linked to a fill it is
``filled`` with the trade's ``trade_id`` (core ``link_fill``; the table's CHECK
holds ``filled`` and the trade together). That is the link: nothing is written
into either store to make it (D13) — Research reads Trade's
``GET /strategies/plans?status=filled&source_kind=hypothesis`` and joins on the id.

``source_kind`` is a filter from trade-api 0.11.0 / core 0.53.0 (TD-178): the
500-row cap then counts hypothesis plans only. An older trade-api ignores the
name and answers the newest filled plans of every kind, so the kind is still
checked here (``links_from_plans``) and ``truncated`` still says when the cap
was reached.

``research.hypothesis.linked_opportunity_ids`` stays as it is: only Research's
own create / patch write it, and on 2026-10-06 it was empty on all 91 rows.

Each Trade environment has its own plans and trades, so the reader names the
environment it reads (``trade_env``, default ``prod`` — the one the deployment's
``TRADE_API_STRATEGY_URL`` points at). A DEV trade id is not a PROD trade id.

Read-only. A failed read is said (``error``), never read as "no trades".
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any

from bifrost_research.mcp.tools._trade_api_client import base_strategy, get, list_items

logger = logging.getLogger(__name__)

TRADE_ENVS = ("dev", "stg", "prod")
PLANS_PATH = "/strategies/plans"
# trade-api's PLANS_LIMIT_MAX. Filled plans on 2026-10-06: PROD 0 · STG 0 · DEV 0.
PLANS_LIMIT = 500
# Sent to trade-api (TD-178); ignored by one before 0.11.0, filtered here again either way.
PLANS_QUERY: dict[str, Any] = {"status": "filled", "source_kind": "hypothesis", "limit": PLANS_LIMIT}
# An overlay on the hypothesis read, not the read itself: fail soft and fast.
TIMEOUT_S = 3.0
CACHE_TTL_S = 60.0

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def strategy_base(trade_env: str) -> str:
    """The Trade API that serves ``env``'s plans. PROD is the deployment's
    ``TRADE_API_STRATEGY_URL``; DEV and STG read ``TRADE_API_STRATEGY_URL_DEV`` /
    ``_STG``, else the env's api-account Service."""
    env = trade_env.strip().lower()
    if env not in TRADE_ENVS:
        raise ValueError(f"trade_env must be one of {', '.join(TRADE_ENVS)}")
    if env == "prod":
        return base_strategy()
    default = f"http://api-account.bifrost-{env}.svc.cluster.local:8769"
    return (os.environ.get(f"TRADE_API_STRATEGY_URL_{env.upper()}") or default).rstrip("/")


def links_from_plans(plans: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Hypothesis id → the trades its filled plans became, oldest plan first.
    A plan that is not filled (draft, intended, cancelled) or names no trade is
    not a link."""
    out: dict[str, list[dict[str, Any]]] = {}
    for p in plans:
        if p.get("source_kind") != "hypothesis" or p.get("status") != "filled":
            continue
        ref = str(p.get("source_ref") or "").strip()
        trade_id = p.get("trade_id")
        if not ref or trade_id is None:
            continue
        out.setdefault(ref, []).append(
            {
                "trade_id": int(trade_id),
                "strategy_plan_id": p.get("strategy_plan_id"),
                "symbol": p.get("symbol"),
                "structure_label": p.get("structure_label"),
            }
        )
    for links in out.values():
        links.sort(key=lambda link: (link["strategy_plan_id"] is None, link["strategy_plan_id"] or 0))
    return out


def read_trade_links(trade_env: str = "prod", *, use_cache: bool = True) -> dict[str, Any]:
    """``{trade_env, source, links, plans_read, truncated, error}`` for one Trade
    environment. ``truncated`` is true when the read came back at the limit — the
    oldest filled hypothesis plans (or, from a trade-api before 0.11.0, the oldest
    filled plans of any kind) may be missing, so an absent link is then not a fact."""
    env = trade_env.strip().lower()
    now = time.monotonic()
    if use_cache:
        with _lock:
            hit = _cache.get(env)
        if hit is not None and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    base = strategy_base(env)
    reading: dict[str, Any] = {
        "trade_env": env,
        "source": f"trade-api {PLANS_PATH}?status=filled&source_kind=hypothesis ({env})",
        "links": {},
        "plans_read": 0,
        "truncated": False,
        "error": None,
    }
    try:
        payload = get(base, PLANS_PATH, dict(PLANS_QUERY), timeout=TIMEOUT_S)
        plans = list_items(payload)
        reading["links"] = links_from_plans(plans)
        reading["plans_read"] = len(plans)
        reading["truncated"] = len(plans) >= PLANS_LIMIT
    except Exception as exc:
        logger.warning("hypothesis trade links (%s) unread: %s", env, exc)
        reading["error"] = str(exc)
    with _lock:
        _cache[env] = (now, reading)
    return reading


def attach_trade_links(rows: list[dict[str, Any]], trade_env: str = "prod") -> dict[str, Any]:
    """Add ``linked_trade_ids`` / ``linked_trades`` (null when the read failed) and
    ``trade_link_basis`` to each hypothesis row; returns the basis."""
    reading = read_trade_links(trade_env)
    basis = {k: reading[k] for k in ("trade_env", "source", "plans_read", "truncated", "error")}
    failed = reading["error"] is not None
    for row in rows:
        links = reading["links"].get(str(row.get("id") or ""), [])
        row["linked_trades"] = None if failed else links
        row["linked_trade_ids"] = None if failed else [link["trade_id"] for link in links]
        row["trade_link_basis"] = basis
    return basis


__all__ = [
    "CACHE_TTL_S",
    "PLANS_LIMIT",
    "PLANS_QUERY",
    "TRADE_ENVS",
    "attach_trade_links",
    "links_from_plans",
    "read_trade_links",
    "strategy_base",
]
