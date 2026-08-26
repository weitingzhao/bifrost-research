"""Morning Prep Agent — Wave RS-E3.2.

Produces one ``morning_brief`` draft per active hypothesis + one global
discoveries draft. Heuristic-first (offline tests); optional LLM enrichment.
Never mutates ``research.hypothesis``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

from bifrost_research.copilot.agents._context import (
    gather_discoveries,
    gather_symbol_context,
    utc_now_iso,
)
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import hypothesis as hyp_repo

logger = logging.getLogger(__name__)

AGENT_ID = "morning_agent"
ACTION_SOURCE = "morning_agent"


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def _dry_run() -> bool:
    return os.environ.get("BIFROST_MORNING_AGENT_DRY_RUN", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
    )


def _heuristic_hypothesis_brief(hyp: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    symbols = hyp.get("symbols") or []
    title = hyp.get("title") or hyp.get("id")
    bullets: list[str] = []
    bullets.append(f"Hypothesis **{title}** remains active ({', '.join(symbols) or 'no symbols'}).")

    sym_ctx = (ctx.get("symbols") or {})
    has_data = False
    for sym in symbols[:4]:
        bucket = sym_ctx.get(sym) or {}
        vrp = bucket.get("vrp")
        regime = bucket.get("regime")
        events = bucket.get("events") or []
        if vrp:
            has_data = True
            pct = vrp.get("vrp_pct_252d")
            pct_s = f"{pct:.0f}th pct" if isinstance(pct, (int, float)) else "n/a"
            bullets.append(
                f"{sym}: VRP20={vrp.get('vrp_20d')} ({pct_s}); "
                f"ATM IV30={vrp.get('atm_iv_30d')}; RV20={vrp.get('rv_20d')}."
            )
        if regime:
            has_data = True
            bullets.append(
                f"{sym}: terrain regime={regime.get('regime')} spot={regime.get('spot')}."
            )
        if events:
            has_data = True
            titles = [str(e.get("title") or e.get("event_id")) for e in events[:2]]
            bullets.append(f"{sym}: recent events — {'; '.join(titles)}.")

    if not has_data:
        bullets.append("No fresh VRP/regime/event data available; review Labs when engines catch up.")

    markdown = "\n".join(f"- {b}" for b in bullets)
    return {
        "markdown": markdown,
        "bullets": bullets,
        "hypothesis_id": hyp.get("id"),
        "hypothesis_title": title,
        "symbols": symbols,
        "model": "heuristic",
        "generated_at": utc_now_iso(),
    }


def _heuristic_global_brief(discoveries: list[dict[str, Any]]) -> dict[str, Any]:
    if not discoveries:
        bullets = [
            "No SEPA / event-radar discoveries available this morning.",
            "Check Daily Brief and Discovery pages after CronJobs finish.",
            "Pin an active hypothesis so Morning Prep can attach per-thesis briefs.",
        ]
    else:
        bullets = []
        for d in discoveries[:5]:
            src = d.get("source", "?")
            if src == "sepa":
                bullets.append(
                    f"SEPA: {d.get('symbol')} score={d.get('sepa_score')} "
                    f"grade={d.get('grade')} stage={d.get('stage')}."
                )
            else:
                bullets.append(
                    f"Event: {d.get('title') or d.get('event_id')} "
                    f"({d.get('event_type')}) symbols={d.get('affected_symbols')}."
                )
    markdown = "## Today's Discoveries\n\n" + "\n".join(f"- {b}" for b in bullets)
    return {
        "markdown": markdown,
        "bullets": bullets,
        "discoveries": discoveries,
        "model": "heuristic",
        "generated_at": utc_now_iso(),
        "create_hypothesis": False,
    }


def _optional_llm_enrich(prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
    """Try provider when API key present; otherwise keep heuristic payload."""
    model = os.environ.get("BIFROST_AGENT_MODEL", "claude-sonnet-4-20250514")
    try:
        from bifrost_research.copilot.providers import resolve_provider

        provider = resolve_provider(model)
        turn = provider.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Bifrost Research Morning Prep. "
                        "Reply with a short 3-bullet status update in plain text. "
                        "No order placement. Research-only."
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
        bullets = [ln.lstrip("-•* ").strip() for ln in text.splitlines() if ln.strip()]
        if not bullets:
            bullets = [text]
        enriched = dict(fallback)
        enriched["markdown"] = "\n".join(f"- {b}" for b in bullets[:6])
        enriched["bullets"] = bullets[:6]
        enriched["model"] = model
        enriched["llm_enriched"] = True
        return enriched
    except Exception as exc:  # noqa: BLE001
        logger.debug("LLM enrich skipped: %s", exc)
        return fallback


def run_morning_prep(
    conn: _Connection | None = None,
    *,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Run Morning Prep. Returns summary with drafts (or dry-run payloads)."""
    is_dry = _dry_run() if dry_run is None else dry_run
    owns_conn = False
    if conn is None and not is_dry:
        from bifrost_research.db.conn import connect

        conn = connect()
        owns_conn = True

    drafts_out: list[dict[str, Any]] = []
    try:
        if is_dry or conn is None:
            # Offline dry-run: fake one hypothesis brief + global
            fake_hyp = {
                "id": "dry-hyp-1",
                "title": "Dry-run hypothesis",
                "symbols": ["SPY"],
                "status": "active",
            }
            ctx = {"symbols": {}, "as_of": utc_now_iso()}
            hyp_payload = _heuristic_hypothesis_brief(fake_hyp, ctx)
            global_payload = _heuristic_global_brief([])
            result = {
                "ok": True,
                "dry_run": True,
                "drafts": [
                    {
                        "kind": "morning_brief",
                        "scope": fake_hyp["id"],
                        "payload": hyp_payload,
                    },
                    {
                        "kind": "morning_brief",
                        "scope": "global",
                        "payload": global_payload,
                    },
                ],
                "count": 2,
            }
            print(json.dumps(result, indent=2, default=str))
            return result

        active = hyp_repo.list_hypotheses(conn, status="active", limit=100)
        for hyp in active:
            symbols = list(hyp.get("symbols") or [])
            ctx = gather_symbol_context(conn, symbols)
            payload = _heuristic_hypothesis_brief(hyp, ctx)
            prompt = (
                f"Hypothesis: {hyp.get('title')}\nThesis: {hyp.get('thesis')}\n"
                f"Context JSON: {json.dumps(ctx, default=str)[:4000]}\n"
                "Produce a 3-bullet morning status update."
            )
            payload = _optional_llm_enrich(prompt, payload)

            action = action_repo.insert_action(
                conn,
                action_kind="draft_hypothesis",
                action_source=ACTION_SOURCE,
                model=payload.get("model"),
                input_payload={"hypothesis_id": hyp["id"], "context": ctx},
                output_payload=payload,
                status="proposed",
            )
            draft = draft_repo.insert_draft(
                conn,
                kind="morning_brief",
                payload=payload,
                scope=str(hyp["id"]),
                generated_by=AGENT_ID,
                linked_action_id=action["id"],
            )
            drafts_out.append(draft)

        discoveries = gather_discoveries(conn, limit=5)
        global_payload = _heuristic_global_brief(discoveries)
        global_prompt = (
            "Summarize top discoveries for morning brief:\n"
            + json.dumps(discoveries, default=str)[:3000]
        )
        global_payload = _optional_llm_enrich(global_prompt, global_payload)
        g_action = action_repo.insert_action(
            conn,
            action_kind="draft_hypothesis",
            action_source=ACTION_SOURCE,
            model=global_payload.get("model"),
            input_payload={"scope": "global", "discoveries": discoveries},
            output_payload=global_payload,
            status="proposed",
        )
        g_draft = draft_repo.insert_draft(
            conn,
            kind="morning_brief",
            payload=global_payload,
            scope="global",
            generated_by=AGENT_ID,
            linked_action_id=g_action["id"],
        )
        drafts_out.append(g_draft)

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
