"""EOD Review Agent — Wave RS-E3.3.

For each active hypothesis, drafts an ``eod_verdict`` with a proposed status
(keep / validated / rejected). Does not apply the status until user approves.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

from bifrost_research.copilot.agents._context import (
    gather_symbol_context,
    utc_now_iso,
)
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import hypothesis as hyp_repo

logger = logging.getLogger(__name__)

AGENT_ID = "eod_agent"
ACTION_SOURCE = "eod_agent"

_PROPOSED_STATUSES = frozenset({"active", "validated", "rejected"})


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def _dry_run() -> bool:
    return os.environ.get("BIFROST_EOD_AGENT_DRY_RUN", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
    )


def _heuristic_verdict(hyp: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    symbols = hyp.get("symbols") or []
    title = hyp.get("title") or hyp.get("id")
    sym_ctx = ctx.get("symbols") or {}
    material = False
    notes: list[str] = []

    for sym in symbols[:4]:
        bucket = sym_ctx.get(sym) or {}
        vrp = bucket.get("vrp")
        events = bucket.get("events") or []
        regime = bucket.get("regime")
        if vrp and isinstance(vrp.get("vrp_pct_252d"), (int, float)):
            pct = float(vrp["vrp_pct_252d"])
            if pct >= 90 or pct <= 10:
                material = True
                notes.append(f"{sym} VRP percentile extreme ({pct:.0f}).")
        if events:
            material = True
            notes.append(f"{sym} has {len(events)} recent event-radar hit(s).")
        if regime and regime.get("regime"):
            notes.append(f"{sym} regime={regime.get('regime')}.")

    if not material:
        proposed = "active"
        rationale = "No material change in VRP extremes or events today; keep active."
        bullets = [
            f"Hypothesis **{title}**: no material change.",
            rationale,
            "Re-check Labs tomorrow or after next CronJob wave.",
        ]
    else:
        # Heuristic: extreme VRP or events → suggest keep with attention note
        # (never auto-validate/reject without stronger signal — Owner decides)
        proposed = "active"
        rationale = (
            "Material signals observed; recommend keep active and review evidence. "
            + (" ".join(notes[:2]) if notes else "")
        ).strip()
        bullets = [
            f"Hypothesis **{title}**: material signals today.",
            rationale,
            "Proposed status remains **active** pending Owner review "
            "(set validated/rejected manually if evidence warrants).",
        ]

    markdown = "\n".join(f"- {b}" for b in bullets)
    return {
        "markdown": markdown,
        "bullets": bullets,
        "hypothesis_id": hyp.get("id"),
        "hypothesis_title": title,
        "symbols": symbols,
        "proposed_status": proposed,
        "rationale": rationale,
        "notes": notes,
        "model": "heuristic",
        "generated_at": utc_now_iso(),
    }


def _optional_llm_enrich(prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
    model = os.environ.get("BIFROST_AGENT_MODEL", "claude-sonnet-4-20250514")
    try:
        from bifrost_research.copilot.providers import resolve_provider

        provider = resolve_provider(model)
        turn = provider.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Bifrost Research EOD Review. "
                        "Propose one of: keep active / promote to validated / demote to rejected. "
                        "Reply with: STATUS=<active|validated|rejected> then 1-2 sentence rationale. "
                        "No order placement."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            tools=[],
            model=model,
        )
        if turn.error or not (turn.text or "").strip():
            return fallback
        text = turn.text.strip()
        proposed = fallback.get("proposed_status", "active")
        upper = text.upper()
        if "STATUS=VALIDATED" in upper or "PROMOTE TO VALIDATED" in upper:
            proposed = "validated"
        elif "STATUS=REJECTED" in upper or "DEMOTE TO REJECTED" in upper:
            proposed = "rejected"
        elif "STATUS=ACTIVE" in upper or "KEEP ACTIVE" in upper:
            proposed = "active"
        if proposed not in _PROPOSED_STATUSES:
            proposed = "active"
        enriched = dict(fallback)
        enriched["proposed_status"] = proposed
        enriched["rationale"] = text
        enriched["markdown"] = f"- Proposed status: **{proposed}**\n- {text}"
        enriched["bullets"] = [f"Proposed status: {proposed}", text]
        enriched["model"] = model
        enriched["llm_enriched"] = True
        return enriched
    except Exception as exc:  # noqa: BLE001
        logger.debug("EOD LLM enrich skipped: %s", exc)
        return fallback


def run_eod_review(
    conn: _Connection | None = None,
    *,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    is_dry = _dry_run() if dry_run is None else dry_run
    owns_conn = False
    if conn is None and not is_dry:
        from bifrost_research.db.conn import connect

        conn = connect()
        owns_conn = True

    drafts_out: list[dict[str, Any]] = []
    try:
        if is_dry or conn is None:
            fake_hyp = {
                "id": "dry-hyp-1",
                "title": "Dry-run hypothesis",
                "symbols": ["SPY"],
                "status": "active",
            }
            payload = _heuristic_verdict(fake_hyp, {"symbols": {}})
            result = {
                "ok": True,
                "dry_run": True,
                "drafts": [
                    {
                        "kind": "eod_verdict",
                        "scope": fake_hyp["id"],
                        "payload": payload,
                    }
                ],
                "count": 1,
            }
            print(json.dumps(result, indent=2, default=str))
            return result

        active = hyp_repo.list_hypotheses(conn, status="active", limit=100)
        if not active:
            return {
                "ok": True,
                "dry_run": False,
                "count": 0,
                "draft_ids": [],
                "active_hypotheses": 0,
                "message": "no active hypotheses",
            }

        for hyp in active:
            symbols = list(hyp.get("symbols") or [])
            ctx = gather_symbol_context(conn, symbols)
            payload = _heuristic_verdict(hyp, ctx)
            prompt = (
                f"Hypothesis: {hyp.get('title')}\nThesis: {hyp.get('thesis')}\n"
                f"Context: {json.dumps(ctx, default=str)[:4000]}\n"
                "Propose status update for EOD."
            )
            payload = _optional_llm_enrich(prompt, payload)

            action = action_repo.insert_action(
                conn,
                action_kind="draft_verdict",
                action_source=ACTION_SOURCE,
                model=payload.get("model"),
                input_payload={"hypothesis_id": hyp["id"], "context": ctx},
                output_payload=payload,
                status="proposed",
            )
            draft = draft_repo.insert_draft(
                conn,
                kind="eod_verdict",
                payload=payload,
                scope=str(hyp["id"]),
                generated_by=AGENT_ID,
                linked_action_id=action["id"],
            )
            drafts_out.append(draft)

        return {
            "ok": True,
            "dry_run": False,
            "count": len(drafts_out),
            "draft_ids": [d["id"] for d in drafts_out],
            "active_hypotheses": len(active),
        }
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
