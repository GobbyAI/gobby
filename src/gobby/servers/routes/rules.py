"""
Rule routes for Gobby HTTP server.

Provides CRUD endpoints for standalone rules stored in rule_definitions.
Wraps RuleDefinitionManager with rule-specific filtering and validation.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError

from gobby.mcp_proxy.tools.workflows._rules import (
    create_rule,
    delete_rule,
    get_rule,
    list_rules,
    toggle_rule,
)
from gobby.servers.routes.configuration_context import require_config_snapshot
from gobby.workflows.definitions import split_rule_definition_data
from gobby.workflows.delivery_disposition import (
    DispositionAmbiguousError,
    prepare_rule_definition_for_persist,
)
from gobby.workflows.pipeline_loader import _is_bundled_template

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.definitions.rules import RuleDefinitionManager

logger = logging.getLogger(__name__)


# =============================================================================
# Request models
# =============================================================================


class RuleCreateRequest(BaseModel):
    """Request body for creating a rule."""

    name: str = Field(..., description="Rule name (must be unique)")
    definition: dict[str, Any] = Field(
        ..., description="Rule definition (event, effect, optional when/group/match)"
    )


class RuleUpdateRequest(BaseModel):
    """Request body for updating a rule."""

    definition: dict[str, Any] | None = Field(
        default=None, description="Full rule definition (replaces body + metadata)"
    )
    name: str | None = Field(default=None, description="New rule name (rename)")
    description: str | None = Field(default=None, description="New description")
    enabled: bool | None = Field(default=None, description="New enabled state")
    priority: int | None = Field(default=None, description="New priority")
    tags: list[str] | None = Field(default=None, description="New tags")


class RuleToggleRequest(BaseModel):
    """Request body for toggling a rule."""

    enabled: bool = Field(..., description="New enabled state")


class BulkToggleRequest(BaseModel):
    """Request body for bulk-toggling rules."""

    source: str = Field(..., description="Source filter: 'installed' or 'project'")
    enabled: bool = Field(..., description="New enabled state for all matching rules")


# =============================================================================
# Router
# =============================================================================


def create_rules_router(server: "HTTPServer") -> APIRouter:
    """Create rules router with endpoints bound to server instance."""
    router = APIRouter(prefix="/api/rules", tags=["rules"])

    def _get_manager() -> "RuleDefinitionManager":
        from gobby.storage.definitions.rules import RuleDefinitionManager

        return RuleDefinitionManager(server.services.database)

    def _rule_body(row: Any) -> dict[str, Any]:
        payload = row.definition_json
        if isinstance(payload, dict):
            body = payload
        else:
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                raise TypeError("rule definition must be a JSON object")
            body = parsed
        if "event" not in body:
            raise TypeError("rule definition missing event")
        return body

    async def _broadcast_rule(event: str, definition_id: str, **kwargs: Any) -> None:
        """Broadcast a rule event via WebSocket if available."""
        try:
            ws = getattr(server.services, "websocket_server", None)
            if ws and hasattr(ws, "broadcast_workflow_event"):
                await ws.broadcast_workflow_event(event, definition_id, **kwargs)
        except Exception as e:
            logger.warning("Failed to broadcast rule event %s: %s", event, e, exc_info=True)

    # -----------------------------------------------------------------
    # GET /api/rules/groups (must be before /{name} to avoid conflict)
    # -----------------------------------------------------------------

    @router.get("/groups")
    async def list_groups() -> dict[str, Any]:
        """List distinct rule groups."""
        try:
            manager = _get_manager()
            rows = await server.run_db(manager.list_all)
            groups: set[str] = set()
            for row in rows:
                try:
                    body = _rule_body(row)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Skipping unparseable rule '%s': %s", row.name, e)
                    continue
                group = body.get("group")
                if group:
                    groups.add(group)
            return {
                "status": "success",
                "groups": sorted(groups),
            }
        except Exception as e:
            logger.exception("Error listing rule groups")
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/tags")
    async def list_tags() -> dict[str, Any]:
        """List distinct rule tags."""
        try:
            manager = _get_manager()
            rows = await server.run_db(manager.list_all)
            tags: set[str] = set()
            for row in rows:
                try:
                    _rule_body(row)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Skipping unparseable rule '%s': %s", row.name, e)
                    continue
                for tag in row.tags or []:
                    tags.add(tag)
            return {
                "status": "success",
                "tags": sorted(tags),
            }
        except Exception as e:
            logger.exception("Error listing rule tags")
            raise HTTPException(status_code=500, detail="Internal server error") from e

    # -----------------------------------------------------------------
    # GET /api/rules
    # -----------------------------------------------------------------

    @router.get("")
    async def list_rules_endpoint(
        event: str | None = Query(None, description="Filter by event type"),
        group: str | None = Query(None, description="Filter by group"),
        enabled: bool | None = Query(None, description="Filter by enabled status"),
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> dict[str, Any]:
        """List rules with optional filters."""
        try:
            manager = _get_manager()
            result = await server.run_db(
                list_rules,
                manager,
                event=event,
                group=group,
                enabled=enabled,
                project_id=project_id,
            )
            config_values = require_config_snapshot(server).active_values
            enforcement = config_values.get("rules.enforcement_enabled", True)
            aggregate_blocks = config_values.get("rules.aggregate_blocks", True)
            return {
                "status": "success",
                "rules": result["rules"],
                "count": result["count"],
                "enforcement_enabled": enforcement is not False,
                "aggregate_blocks": aggregate_blocks is not False,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Error listing rules")
            raise HTTPException(status_code=500, detail="Internal server error") from e

    # -----------------------------------------------------------------
    # POST /api/rules
    # -----------------------------------------------------------------

    @router.post("", status_code=201)
    async def create_rule_endpoint(request: RuleCreateRequest) -> dict[str, Any]:
        """Create a new rule."""
        manager = _get_manager()
        result = await server.run_db(
            create_rule, manager, name=request.name, definition=request.definition
        )

        if not result["success"]:
            error = result["error"]
            if "already exists" in error.lower():
                raise HTTPException(status_code=409, detail=error)
            raise HTTPException(status_code=400, detail=error)

        rule_id = result["rule"].get("id", "")
        await _broadcast_rule("rule_created", rule_id)

        return {"status": "success", "rule": result["rule"]}

    # -----------------------------------------------------------------
    # PUT /api/rules/bulk-toggle
    # -----------------------------------------------------------------

    @router.put("/bulk-toggle")
    async def bulk_toggle_rules(request: BulkToggleRequest) -> dict[str, Any]:
        """Toggle all rules matching a source filter."""
        if request.source not in ("installed", "project"):
            raise HTTPException(status_code=400, detail="source must be 'installed' or 'project'")
        try:
            manager = _get_manager()
            rows = await server.run_db(manager.list_all, include_deleted=False)
            count = 0
            failures: list[dict[str, str]] = []
            for row in rows:
                if row.source == request.source:
                    try:
                        await server.run_db(manager.update, row.id, enabled=request.enabled)
                    except Exception as e:
                        logger.warning(
                            "Failed to bulk-toggle rule",
                            extra={
                                "rule_id": row.id,
                                "rule_name": row.name,
                                "source": row.source,
                            },
                            exc_info=True,
                        )
                        failures.append(
                            {
                                "rule_id": row.id,
                                "rule_name": row.name,
                                "source": row.source,
                                "error": str(e),
                            }
                        )
                        continue
                    count += 1
            return {
                "status": "success",
                "count": count,
                "failures": failures,
                "partial": bool(failures),
            }
        except Exception as e:
            logger.exception("Error in bulk toggle")
            raise HTTPException(status_code=500, detail="Internal server error") from e

    # -----------------------------------------------------------------
    # GET /api/rules/{name}
    # -----------------------------------------------------------------

    @router.get("/{name}")
    async def get_rule_endpoint(name: str) -> dict[str, Any]:
        """Get a rule by name."""
        manager = _get_manager()
        result = await server.run_db(get_rule, manager, name=name)

        if not result["success"]:
            raise HTTPException(status_code=404, detail=result["error"])

        return {"status": "success", "rule": result["rule"]}

    # -----------------------------------------------------------------
    # PUT /api/rules/{name}
    # -----------------------------------------------------------------

    @router.put("/{name}")
    async def update_rule_endpoint(name: str, request: RuleUpdateRequest) -> dict[str, Any]:
        """Update rule fields."""
        manager = _get_manager()

        row = await server.run_db(manager.get_by_name, name)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Rule '{name}' not found")

        fields = request.model_dump(exclude_unset=True)

        # Handle full definition replacement from YAML editor
        definition = fields.pop("definition", None)
        if definition is not None:
            try:
                definition, embedded_metadata = split_rule_definition_data(definition)
                definition = prepare_rule_definition_for_persist(name, definition)
            except ValidationError as e:
                raise HTTPException(status_code=400, detail=f"Invalid rule definition: {e}") from e
            except DispositionAmbiguousError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

            for key, value in embedded_metadata.items():
                if key != "name" and key not in fields:
                    fields[key] = value

            fields["definition_json"] = json.dumps(definition)

        new_name = fields.get("name")
        if new_name is not None and new_name != row.name:
            if _is_bundled_template(row):
                raise HTTPException(
                    status_code=400,
                    detail="Bundled template rules cannot be renamed",
                )
            existing_rule = next(
                (
                    candidate
                    for candidate in await server.run_db(manager.list_all)
                    if candidate.name == new_name and candidate.id != row.id
                ),
                None,
            )
            if existing_rule is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Rule '{new_name}' already exists",
                )

        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")

        try:
            updated = await server.run_db(manager.update, row.id, **fields)
        except Exception as e:
            logger.exception(
                "Failed to update rule %s (%s): %s",
                row.id,
                row.name,
                e,
            )
            raise HTTPException(status_code=500, detail="Internal server error") from e

        try:
            body = _rule_body(updated)
        except (TypeError, json.JSONDecodeError):
            logger.warning(
                "Updated rule %s (%s) has an unparseable definition; returning metadata only",
                updated.id,
                updated.name,
            )
            body = {}
        return {
            "status": "success",
            "rule": {
                "id": updated.id,
                "name": updated.name,
                "event": body.get("event"),
                "effects": body.get("effects")
                or ([body["effect"]] if body.get("effect") else None),
                "group": body.get("group"),
                "when": body.get("when"),
                "match": body.get("match"),
                "enabled": updated.enabled,
                "priority": updated.priority,
                "description": updated.description,
                "source": updated.source,
                "tags": updated.tags,
            },
        }

    # -----------------------------------------------------------------
    # DELETE /api/rules/{name}
    # -----------------------------------------------------------------

    @router.delete("/{name}")
    async def delete_rule_endpoint(
        name: str,
        force: bool = Query(False, description="Override bundled protection"),
    ) -> dict[str, Any]:
        """Soft-delete a rule. Bundled rules are protected unless force=True."""
        manager = _get_manager()
        result = await server.run_db(delete_rule, manager, name=name, force=force)

        if not result["success"]:
            error = result["error"]
            if "not found" in error.lower():
                raise HTTPException(status_code=404, detail=error)
            if "bundled" in error.lower():
                raise HTTPException(status_code=403, detail=error)
            raise HTTPException(status_code=400, detail=error)

        return {"status": "success", "deleted": result["deleted"]}

    # -----------------------------------------------------------------
    # PUT /api/rules/{name}/toggle
    # -----------------------------------------------------------------

    @router.put("/{name}/toggle")
    async def toggle_rule_endpoint(name: str, request: RuleToggleRequest) -> dict[str, Any]:
        """Toggle a rule's enabled state."""
        manager = _get_manager()
        result = await server.run_db(toggle_rule, manager, name=name, enabled=request.enabled)

        if not result["success"]:
            raise HTTPException(status_code=404, detail=result["error"])

        return {"status": "success", "rule": result["rule"]}

    return router
