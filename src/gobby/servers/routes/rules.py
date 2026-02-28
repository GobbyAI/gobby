"""
Rule routes for Gobby HTTP server.

Provides CRUD endpoints for standalone rules stored as workflow_definitions
with workflow_type='rule'. Wraps LocalWorkflowDefinitionManager with
rule-specific filtering and validation.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from gobby.mcp_proxy.tools.workflows._rules import (
    create_rule,
    delete_rule,
    get_rule,
    list_rules,
    toggle_rule,
)
from gobby.storage.config_store import ConfigStore
from gobby.utils.metrics import get_metrics_collector
from gobby.workflows.definitions import RuleDefinitionBody

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

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
    description: str | None = Field(default=None, description="New description")
    enabled: bool | None = Field(default=None, description="New enabled state")
    priority: int | None = Field(default=None, description="New priority")
    tags: list[str] | None = Field(default=None, description="New tags")


class RulesCollectionUpdate(BaseModel):
    """Request body for updating rules collection settings."""

    enforcement_enabled: bool | None = Field(default=None, description="Global enforcement toggle")


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
    metrics = get_metrics_collector()

    def _get_manager() -> "LocalWorkflowDefinitionManager":
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        return LocalWorkflowDefinitionManager(server.services.database)

    async def _broadcast_rule(event: str, definition_id: str, **kwargs: Any) -> None:
        """Broadcast a rule event via WebSocket if available."""
        try:
            ws = getattr(server.services, "websocket_server", None)
            if ws and hasattr(ws, "broadcast_workflow_event"):
                await ws.broadcast_workflow_event(event, definition_id, **kwargs)
        except Exception as e:
            logger.debug(f"Failed to broadcast rule event {event}: {e}")

    # -----------------------------------------------------------------
    # GET /api/rules/groups (must be before /{name} to avoid conflict)
    # -----------------------------------------------------------------

    @router.get("/groups")
    async def list_groups() -> dict[str, Any]:
        """List distinct rule groups."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            rows = manager.list_all(workflow_type="rule")
            groups: set[str] = set()
            for row in rows:
                body = json.loads(row.definition_json)
                group = body.get("group")
                if group:
                    groups.add(group)
            return {
                "status": "success",
                "groups": sorted(groups),
            }
        except Exception as e:
            logger.exception("Error listing rule groups")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/tags")
    async def list_tags() -> dict[str, Any]:
        """List distinct rule tags."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            rows = manager.list_all(workflow_type="rule")
            tags: set[str] = set()
            for row in rows:
                for tag in row.tags or []:
                    tags.add(tag)
            return {
                "status": "success",
                "tags": sorted(tags),
            }
        except Exception as e:
            logger.exception("Error listing rule tags")
            raise HTTPException(status_code=500, detail=str(e)) from e

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
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            result = list_rules(
                manager,
                event=event,
                group=group,
                enabled=enabled,
                project_id=project_id,
                include_templates=True,
            )
            config_store = ConfigStore(server.services.database)
            enforcement = config_store.get("rules.enforcement_enabled")
            return {
                "status": "success",
                "rules": result["rules"],
                "count": result["count"],
                "enforcement_enabled": enforcement is not False,
            }
        except Exception as e:
            logger.exception("Error listing rules")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # -----------------------------------------------------------------
    # POST /api/rules
    # -----------------------------------------------------------------

    @router.post("", status_code=201)
    async def create_rule_endpoint(request: RuleCreateRequest) -> dict[str, Any]:
        """Create a new rule."""
        metrics.inc_counter("http_requests_total")
        manager = _get_manager()
        result = create_rule(manager, name=request.name, definition=request.definition)

        if not result["success"]:
            error = result["error"]
            if "already exists" in error.lower():
                raise HTTPException(status_code=409, detail=error)
            raise HTTPException(status_code=400, detail=error)

        rule_id = result["rule"].get("id", "")
        await _broadcast_rule("rule_created", rule_id)

        return {"status": "success", "rule": result["rule"]}

    # -----------------------------------------------------------------
    # PUT /api/rules  (collection-level: enforcement toggle)
    # -----------------------------------------------------------------

    @router.put("")
    async def update_rules_collection(request: RulesCollectionUpdate) -> dict[str, Any]:
        """Update rules collection settings (e.g. global enforcement toggle)."""
        metrics.inc_counter("http_requests_total")
        config_store = ConfigStore(server.services.database)
        if request.enforcement_enabled is not None:
            config_store.set("rules.enforcement_enabled", request.enforcement_enabled)
        val = config_store.get("rules.enforcement_enabled")
        return {"status": "success", "enforcement_enabled": val is not False}

    # -----------------------------------------------------------------
    # PUT /api/rules/bulk-toggle
    # -----------------------------------------------------------------

    @router.put("/bulk-toggle")
    async def bulk_toggle_rules(request: BulkToggleRequest) -> dict[str, Any]:
        """Toggle all rules matching a source filter."""
        metrics.inc_counter("http_requests_total")
        if request.source not in ("installed", "project"):
            raise HTTPException(status_code=400, detail="source must be 'installed' or 'project'")
        try:
            manager = _get_manager()
            rows = manager.list_all(workflow_type="rule", include_deleted=False)
            count = 0
            for row in rows:
                if row.source == request.source:
                    manager.update(row.id, enabled=request.enabled)
                    count += 1
            return {"status": "success", "count": count}
        except Exception as e:
            logger.exception("Error in bulk toggle")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # -----------------------------------------------------------------
    # GET /api/rules/{name}
    # -----------------------------------------------------------------

    @router.get("/{name}")
    async def get_rule_endpoint(name: str) -> dict[str, Any]:
        """Get a rule by name."""
        metrics.inc_counter("http_requests_total")
        manager = _get_manager()
        result = get_rule(manager, name=name)

        if not result["success"]:
            raise HTTPException(status_code=404, detail=result["error"])

        return {"status": "success", "rule": result["rule"]}

    # -----------------------------------------------------------------
    # PUT /api/rules/{name}
    # -----------------------------------------------------------------

    @router.put("/{name}")
    async def update_rule_endpoint(name: str, request: RuleUpdateRequest) -> dict[str, Any]:
        """Update rule fields."""
        metrics.inc_counter("http_requests_total")
        manager = _get_manager()

        row = manager.get_by_name(name)
        if row is None or row.workflow_type != "rule":
            raise HTTPException(status_code=404, detail=f"Rule '{name}' not found")

        fields = request.model_dump(exclude_unset=True)

        # Handle full definition replacement from YAML editor
        definition = fields.pop("definition", None)
        if definition is not None:
            # Validate the rule body
            try:
                RuleDefinitionBody.model_validate(definition)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid rule definition: {e}") from e

            # Extract row-level metadata from definition into fields (don't override explicit values)
            for key in ("description", "enabled", "priority", "tags"):
                if key not in fields and key in definition:
                    fields[key] = definition.pop(key)

            # Strip non-body keys (e.g. name) from definition before serializing
            definition.pop("name", None)

            fields["definition_json"] = json.dumps(definition)

        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")

        try:
            updated = manager.update(row.id, **fields)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

        body = json.loads(updated.definition_json)
        return {
            "status": "success",
            "rule": {
                "id": updated.id,
                "name": updated.name,
                "event": body.get("event"),
                "effect": body.get("effect"),
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
        metrics.inc_counter("http_requests_total")
        manager = _get_manager()
        result = delete_rule(manager, name=name, force=force)

        if not result["success"]:
            error = result["error"]
            if "not found" in error.lower():
                raise HTTPException(status_code=404, detail=error)
            if "bundled" in error.lower() or "template" in error.lower():
                raise HTTPException(status_code=403, detail=error)
            raise HTTPException(status_code=400, detail=error)

        return {"status": "success", "deleted": result["deleted"]}

    # -----------------------------------------------------------------
    # PUT /api/rules/{name}/toggle
    # -----------------------------------------------------------------

    @router.put("/{name}/toggle")
    async def toggle_rule_endpoint(name: str, request: RuleToggleRequest) -> dict[str, Any]:
        """Toggle a rule's enabled state."""
        metrics.inc_counter("http_requests_total")
        manager = _get_manager()
        result = toggle_rule(manager, name=name, enabled=request.enabled)

        if not result["success"]:
            raise HTTPException(status_code=404, detail=result["error"])

        return {"status": "success", "rule": result["rule"]}

    return router
