"""Admin CRUD + status endpoints for rate-limit definitions (issue #295).

Definitions live in the ``mcp_rate_limits`` collection and are enforced at the
auth-server ``/validate`` hop. All mutating endpoints are admin-only, mirroring
``m2m_management_routes.py``. The ``_id`` is always derived server-side from the
definition body (``<axis>:<entity_type>:<name>:<window_seconds>``); the URL id on
PUT is only a consistency check, never parsed into fields (names may contain
colons).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

from registry.audit.context import set_audit_action
from registry.auth.dependencies import nginx_proxied_auth
from registry.rate_limiting.definitions_repository import DefinitionsRepository
from registry.rate_limiting.models import (
    UNENFORCED_ENTITY_TYPES,
    RateLimitDefinition,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_RESOURCE_TYPE: str = "rate_limit"


def _require_admin(
    user_context: dict | None,
) -> None:
    """Enforce admin permission or raise 401/403."""
    if not user_context:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user_context.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator permissions are required")


def _validate_enforceable(
    definition: RateLimitDefinition,
) -> None:
    """Reject definitions whose entity_type is not yet enforced (fail-closed footgun guard).

    ``mcp_tool`` / ``a2a_skill`` are modeled but their classifier is a later phase,
    so accepting them would create a silently-inert limit. Reject with a clear message.
    """
    if definition.entity_type in UNENFORCED_ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"entity_type '{definition.entity_type}' is not enforced in this version "
                f"(tool/skill rate limiting is a later phase)"
            ),
        )


def _parse_definition(
    body: dict,
) -> RateLimitDefinition:
    """Parse a request body into a RateLimitDefinition or raise 400."""
    try:
        return RateLimitDefinition(**body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid rate-limit definition: {exc}"
        ) from exc


_repository: DefinitionsRepository | None = None


def _get_repository() -> DefinitionsRepository:
    """Return a shared DefinitionsRepository singleton for the registry process."""
    global _repository
    if _repository is None:
        _repository = DefinitionsRepository()
    return _repository


class _DeleteResponse(BaseModel):
    """Response body for a delete operation."""

    deleted: bool


@router.get("/rate-limits")
async def list_rate_limits(
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """List all rate-limit definitions (admin only)."""
    _require_admin(user_context)
    set_audit_action(request, "list", _RESOURCE_TYPE)
    definitions = await _get_repository().list_all()
    return {"definitions": [d.model_dump() for d in definitions]}


@router.put("/rate-limits/{definition_id:path}")
async def put_rate_limit(
    definition_id: str,
    body: dict,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """Create or update a rate-limit definition (admin only).

    The ``_id`` is derived from the body fields; the URL ``{definition_id}`` must
    match it exactly or the request is rejected (400). The URL id is never split.
    """
    _require_admin(user_context)
    set_audit_action(request, "update", _RESOURCE_TYPE, resource_id=definition_id)
    definition = _parse_definition(body)
    _validate_enforceable(definition)

    built_id = definition.build_id()
    if built_id != definition_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"URL id '{definition_id}' does not match the definition "
                f"'{built_id}' derived from the body"
            ),
        )

    stored = await _get_repository().upsert(definition)
    return stored.model_dump()


@router.delete("/rate-limits/{definition_id:path}")
async def delete_rate_limit(
    definition_id: str,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> _DeleteResponse:
    """Delete a rate-limit definition by id (admin only)."""
    _require_admin(user_context)
    set_audit_action(request, "delete", _RESOURCE_TYPE, resource_id=definition_id)
    deleted = await _get_repository().delete(definition_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Definition not found")
    return _DeleteResponse(deleted=True)


@router.get("/rate-limits-status")
async def rate_limit_status(
    request: Request,
    identity: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    name: Annotated[str | None, Query()] = None,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """Introspect current definitions for a caller and/or a target entity (admin only).

    Returns the matching definitions (windows and limits) without consuming any
    counter. Counter-value introspection is a later enhancement.
    """
    _require_admin(user_context)
    set_audit_action(request, "read", _RESOURCE_TYPE)
    repository = _get_repository()

    result: dict = {"caller": [], "target": []}
    if identity:
        # The caller's group definitions are what governs them; the caller passes
        # the group name(s) via identity for introspection.
        caller_defs = await repository.list_caller_limits("group", [identity])
        result["caller"] = [d.model_dump() for d in caller_defs]
    if entity_type and name:
        target_defs = await repository.list_target_limits(entity_type, name)
        result["target"] = [d.model_dump() for d in target_defs]
    return result
