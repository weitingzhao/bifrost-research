"""MCP write tools: research.loop.* — Wave C Copilot Runs Loop.

Tools:
  research.loop.propose_candidate
  research.loop.promote_to_hypothesis
  research.loop.attach_backtest_evidence
  research.loop.propose_order_intent
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools._common import with_conn
from bifrost_research.mcp.tools._write_common import (
    WRITE_SUFFIX,
    diff_preview,
    executed_ok,
    require_approval_or_error,
)
from bifrost_research.repositories import candidate_pool as cand_repo
from bifrost_research.repositories import hypothesis as hyp_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import ai_action_log as action_repo


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.loop.propose_candidate",
        description=(
            "Add a symbol to research.candidate_pool (Discover→Analyze bridge). "
            f"{WRITE_SUFFIX}"
        ),
    )
    def propose_candidate(
        symbol: str,
        source: str = "copilot",
        score: float | None = None,
        lens_snapshot: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        source_ref: dict[str, Any] | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        args = {
            "symbol": symbol,
            "source": source,
            "score": score,
            "lens_snapshot": lens_snapshot or {},
            "tags": tags or [],
            "source_ref": source_ref or {},
        }
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.loop.propose_candidate",
            arguments=args,
        )
        if gate is not None:
            return gate

        preview = {**args, "symbol": (symbol or "").strip().upper()}
        impact = {"creates_row": True, "table": "research.candidate_pool", "mutates": ["INSERT"]}
        if dry_run:
            return diff_preview(
                diff_kind="candidate_batch",
                preview={"items": [preview]},
                impact=impact,
                dry_run=True,
            )

        def _run(conn: Any) -> dict[str, Any]:
            row = cand_repo.create_candidate(
                conn,
                symbol=symbol,
                source=source or "copilot",
                score=score,
                lens_snapshot=lens_snapshot,
                tags=tags,
                source_ref=source_ref,
            )
            return executed_ok("candidate_batch", row)

        return with_conn(_run)

    @mcp.tool(
        name="research.loop.promote_to_hypothesis",
        description=(
            "Promote an open candidate into research.hypothesis. "
            f"{WRITE_SUFFIX}"
        ),
    )
    def promote_to_hypothesis(
        candidate_id: str,
        title: str | None = None,
        thesis: str | None = None,
        tags: list[str] | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        args = {
            "candidate_id": candidate_id,
            "title": title,
            "thesis": thesis,
            "tags": tags or [],
        }
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.loop.promote_to_hypothesis",
            arguments=args,
        )
        if gate is not None:
            return gate

        if dry_run:
            return diff_preview(
                diff_kind="hypothesis_draft",
                preview=args,
                impact={
                    "creates_row": True,
                    "table": "research.hypothesis",
                    "mutates": ["INSERT", "UPDATE candidate"],
                },
                dry_run=True,
            )

        def _run(conn: Any) -> dict[str, Any]:
            cand = cand_repo.get_candidate(conn, candidate_id)
            if cand is None:
                return {"ok": False, "error": "candidate not found", "status": 404}
            if cand.get("status") != "open":
                return {
                    "ok": False,
                    "error": f"candidate status is {cand.get('status')}",
                    "status": 400,
                }
            sym = cand["symbol"]
            hyp = hyp_repo.create_hypothesis(
                conn,
                title=(title or f"{sym} candidate").strip(),
                thesis=(
                    thesis
                    or f"Promoted from candidate pool ({cand.get('source')})."
                ).strip(),
                symbols=[sym],
                tags=list(tags or []) + ["from-candidate"],
                origin_page="copilot-loop",
                origin_ref={"candidate_id": candidate_id, "source": "copilot"},
            )
            updated = cand_repo.promote_candidate(
                conn, candidate_id, hypothesis_id=hyp["id"]
            )
            return executed_ok(
                "hypothesis_draft",
                {"candidate": updated, "hypothesis": hyp},
            )

        return with_conn(_run)

    @mcp.tool(
        name="research.loop.attach_backtest_evidence",
        description=(
            "Attach a backtest_run id onto a hypothesis.linked_backtest_ids. "
            f"{WRITE_SUFFIX}"
        ),
    )
    def attach_backtest_evidence(
        hypothesis_id: str,
        backtest_run_id: str,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        args = {
            "hypothesis_id": hypothesis_id,
            "backtest_run_id": backtest_run_id,
        }
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.loop.attach_backtest_evidence",
            arguments=args,
        )
        if gate is not None:
            return gate

        if dry_run:
            return diff_preview(
                diff_kind="attach_backtest_evidence",
                preview=args,
                impact={"creates_row": False, "table": "research.hypothesis", "mutates": ["PATCH"]},
                dry_run=True,
            )

        def _run(conn: Any) -> dict[str, Any]:
            hyp = hyp_repo.get_hypothesis(conn, hypothesis_id)
            if hyp is None:
                return {"ok": False, "error": "hypothesis not found", "status": 404}
            linked = list(hyp.get("linked_backtest_ids") or [])
            if backtest_run_id not in linked:
                linked.append(backtest_run_id)
            updated = hyp_repo.patch_hypothesis(
                conn, hypothesis_id, {"linked_backtest_ids": linked}
            )
            return executed_ok("attach_backtest_evidence", updated)

        return with_conn(_run)

    @mcp.tool(
        name="research.loop.draft_decision",
        description=(
            "Create an ai_draft kind=decision_draft for Owner approval. "
            f"{WRITE_SUFFIX}"
        ),
    )
    def draft_decision(
        hypothesis_id: str,
        verdict: str,
        sizing_hint: dict[str, Any] | None = None,
        risk_hint: dict[str, Any] | None = None,
        rationale: str | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        args = {
            "hypothesis_id": hypothesis_id,
            "verdict": verdict,
            "sizing_hint": sizing_hint or {},
            "risk_hint": risk_hint or {},
            "rationale": rationale or "",
        }
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.loop.draft_decision",
            arguments=args,
        )
        if gate is not None:
            return gate

        if dry_run:
            return diff_preview(
                diff_kind="decision_draft",
                preview=args,
                impact={"creates_row": True, "table": "research.ai_draft", "mutates": ["INSERT"]},
                dry_run=True,
            )

        def _run(conn: Any) -> dict[str, Any]:
            action = action_repo.insert_action(
                conn,
                action_kind="draft_decision",
                action_source="loop_curator",
                input_payload=args,
                output_payload=None,
                status="proposed",
            )
            draft = draft_repo.insert_draft(
                conn,
                kind="decision_draft",
                payload=args,
                scope=f"hypothesis:{hypothesis_id}",
                generated_by="loop_curator",
                linked_action_id=action["id"],
            )
            return executed_ok("decision_draft", {"draft": draft, "action": action})

        return with_conn(_run)

    @mcp.tool(
        name="research.loop.propose_order_intent",
        description=(
            "Create an advisory order_intent draft (D10 BLOCKED — no live orders). "
            f"{WRITE_SUFFIX}"
        ),
    )
    def propose_order_intent(
        hypothesis_id: str,
        strategy_template: str,
        legs: list[dict[str, Any]] | None = None,
        rationale: str = "",
        sizing_hint: dict[str, Any] | None = None,
        risk_hint: dict[str, Any] | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        from bifrost_research.copilot.harness.order_intent_schema import (
            LegSpec,
            OrderIntent,
            RiskHint,
            SizingHint,
        )

        args = {
            "hypothesis_id": hypothesis_id,
            "strategy_template": strategy_template,
            "legs": legs or [],
            "rationale": rationale,
        }
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.loop.propose_order_intent",
            arguments=args,
        )
        if gate is not None:
            return gate

        leg_models = [LegSpec.model_validate(x) for x in (legs or [])]
        intent = OrderIntent(
            hypothesis_id=hypothesis_id,
            strategy_template=strategy_template,
            legs=leg_models,
            sizing_hint=SizingHint.model_validate(sizing_hint or {}),
            risk_hint=RiskHint.model_validate(risk_hint or {}),
            rationale=rationale or "",
        )
        preview = intent.to_payload()

        if dry_run:
            return diff_preview(
                diff_kind="order_intent",
                preview=preview,
                impact={"creates_row": True, "table": "research.ai_draft", "mutates": ["INSERT"]},
                dry_run=True,
            )

        def _run(conn: Any) -> dict[str, Any]:
            action = action_repo.insert_action(
                conn,
                action_kind="order_intent",
                action_source="loop_curator",
                input_payload=preview,
                output_payload=None,
                status="proposed",
            )
            draft = draft_repo.insert_draft(
                conn,
                kind="order_intent",
                payload=preview,
                scope=f"hypothesis:{hypothesis_id}",
                generated_by="loop_curator",
                linked_action_id=action["id"],
                expires_at=intent.expiry_at,
            )
            return executed_ok("order_intent", {"draft": draft, "action": action})

        return with_conn(_run)
