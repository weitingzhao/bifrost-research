"""Loop policy templates — P0-2.

Routes:
    GET    /research/policy-templates
    POST   /research/policy-templates
    POST   /research/policy-templates/validate
    GET    /research/policy-templates/{id}
    PATCH  /research/policy-templates/{id}
    DELETE /research/policy-templates/{id}

The Loop's strategy used to be a constant in two codebases at once, so tuning it
meant a release. These routes make it data. Every write parses through the
runtime's own ``LoopPolicy``, so a template that saves is a template the runtime
honours; the non-fatal warnings come back with it rather than being swallowed.

D-Research-Harness: writes research.* only; D10 BLOCKED — no trade execution.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.db.conn import connect
from bifrost_research.repositories import loop_policy_template as tpl_repo
from bifrost_research.repositories.loop_policy_template import PolicyValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/policy-templates", tags=["research-policy-template"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


class TemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    policy_json: dict[str, Any]
    description: str = ""
    is_default: bool = False
    owner_id: str = "owner"


class TemplatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    policy_json: dict[str, Any] | None = None
    is_default: bool | None = None


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_json: dict[str, Any]


@router.get("")
def list_policy_templates(
    universe_mode: str | None = Query(default=None),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        return _ok({"items": tpl_repo.list_templates(conn, universe_mode=universe_mode)})
    finally:
        conn.close()


@router.post("/validate")
def validate_policy_body(body: PolicyBody) -> dict[str, Any]:
    """Dry-run a policy without saving.

    The console calls this before save so an invalid shape is refused with the
    parser's own message, and so the "min_hit_rate is ignored without
    flag_filter" class of warning is visible while editing rather than discovered
    from a run that quietly filtered nothing.
    """
    try:
        normalised, warnings = tpl_repo.validate_policy(body.policy_json)
    except PolicyValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ok({"policy_json": normalised, "warnings": warnings})


@router.post("")
def create_policy_template(body: TemplateCreate) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = tpl_repo.create_template(
            conn,
            name=body.name,
            policy_json=body.policy_json,
            description=body.description,
            is_default=body.is_default,
            owner_id=body.owner_id,
        )
        _, warnings = tpl_repo.validate_policy(row.get("policy_json") or {})
        return _ok({**row, "warnings": warnings})
    except PolicyValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/{template_id}")
def get_policy_template(template_id: str) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = tpl_repo.get_template(conn, template_id)
        if row is None:
            raise HTTPException(status_code=404, detail="policy template not found")
        _, warnings = tpl_repo.validate_policy(row.get("policy_json") or {})
        return _ok({**row, "warnings": warnings})
    finally:
        conn.close()


@router.patch("/{template_id}")
def patch_policy_template(template_id: str, body: TemplatePatch) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = tpl_repo.update_template(
            conn,
            template_id,
            name=body.name,
            description=body.description,
            policy_json=body.policy_json,
            is_default=body.is_default,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="policy template not found")
        _, warnings = tpl_repo.validate_policy(row.get("policy_json") or {})
        return _ok({**row, "warnings": warnings})
    except PolicyValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@router.delete("/{template_id}")
def delete_policy_template(template_id: str) -> dict[str, Any]:
    """Delete a template nothing was created from.

    Refused while objectives still name it as their source: the link lives in
    objective.policy_json, which no foreign key defends, so without this check the
    delete would succeed and leave those objectives pointing at a template that no
    longer exists.
    """
    conn = _connect_or_503()
    try:
        existing = tpl_repo.get_template(conn, template_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="policy template not found")
        used = tpl_repo.count_objectives_using(conn, template_id)
        if used > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"template is the source of {used} objective(s). "
                    "Objectives keep their own copy of the policy, so deleting this "
                    "would only lose the provenance — rename or edit it instead."
                ),
            )
        return _ok({"id": template_id, "deleted": tpl_repo.delete_template(conn, template_id)})
    finally:
        conn.close()
