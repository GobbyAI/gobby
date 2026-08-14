"""
Workflow definition routes for Gobby HTTP server.

Provides CRUD endpoints for managing workflow definitions in the database.
"""

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

import yaml
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ValidationError

from gobby.workflows.definitions import RuleDefinitionBody

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

logger = logging.getLogger(__name__)


class CreateWorkflowRequest(BaseModel):
    """Request body for creating a workflow definition."""

    name: str
    definition_json: str
    workflow_type: str = "workflow"
    project_id: str | None = None
    description: str | None = None
    version: str = "1.0"
    enabled: bool = True
    priority: int = 100
    sources: list[str] | None = None
    canvas_json: str | None = None
    source: Literal["installed", "agent", "project", "custom"] = "custom"
    tags: list[str] | None = None


class UpdateWorkflowRequest(BaseModel):
    """Request body for updating a workflow definition."""

    name: str | None = None
    definition_json: str | None = None
    description: str | None = None
    version: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    sources: list[str] | None = None
    canvas_json: str | None = None
    tags: list[str] | None = None


class ImportYAMLRequest(BaseModel):
    """Request body for importing a workflow from YAML."""

    yaml_content: str
    project_id: str | None = None


class MoveToProjectRequest(BaseModel):
    """Request body for moving a workflow to project scope."""

    project_id: str


class DuplicateRequest(BaseModel):
    """Request body for duplicating a workflow."""

    new_name: str


def create_workflows_router(server: "HTTPServer") -> APIRouter:
    """
    Create workflows router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with workflow definition endpoints
    """
    router = APIRouter(prefix="/api/workflows", tags=["workflows"])

    def _get_manager() -> "LocalWorkflowDefinitionManager":
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        return LocalWorkflowDefinitionManager(server.services.database)

    def _reject_agent_kind(kind: str | None) -> None:
        if kind == "agent":
            raise HTTPException(
                status_code=400,
                detail="Agent definitions use /api/agents",
            )
        if kind == "rule":
            raise HTTPException(
                status_code=400,
                detail="Rule definitions use /api/rules",
            )
        if kind == "variable":
            raise HTTPException(
                status_code=400,
                detail="Variable definitions use the variable domain MCP tools",
            )
        if kind == "pipeline":
            raise HTTPException(
                status_code=400,
                detail="Pipeline definitions use the pipeline domain MCP tools",
            )

    def _reject_agent_row(definition_id: str) -> None:
        try:
            row = _get_manager().get(definition_id, include_deleted=True)
        except ValueError:
            return
        _reject_agent_kind(row.workflow_type)

    async def _broadcast_workflow(event: str, definition_id: str, **kwargs: Any) -> None:
        """Broadcast a workflow event via WebSocket if available."""
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_workflow_event(event, definition_id, **kwargs)
            except Exception as e:
                logger.debug("Failed to broadcast workflow event %s: %s", event, e)

    @router.get("/templates")
    async def list_templates() -> dict[str, Any]:
        """List available workflow templates for the 'New' button."""
        from gobby.workflows.workflow_templates import get_workflow_templates

        templates = get_workflow_templates()
        return {"status": "success", "templates": templates, "count": len(templates)}

    @router.get("")
    async def list_workflows(
        workflow_type: str | None = Query(None),
        enabled: bool | None = Query(None),
        project_id: str | None = Query(None),
        include_deleted: bool = Query(False),
    ) -> dict[str, Any]:
        """List workflow definitions with optional filters."""
        try:
            _reject_agent_kind(workflow_type)
            manager = _get_manager()
            rows = await server.run_db(
                manager.list_all,
                project_id=project_id,
                workflow_type=workflow_type,
                enabled=enabled,
                include_deleted=include_deleted,
            )
            rows = [
                row
                for row in rows
                if row.workflow_type not in {"agent", "rule", "variable", "pipeline"}
            ]
            definitions = [r.to_dict() for r in rows]

            # Annotate with template drift info
            from gobby.workflows.template_hashes import get_template_hash_cache

            cache = get_template_hash_cache()
            cache.annotate_rows(definitions)

            return {
                "status": "success",
                "definitions": definitions,
                "count": len(rows),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Error listing workflow definitions")
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/{definition_id}/export")
    async def export_workflow(definition_id: str) -> Response:
        """Export a workflow definition as YAML."""
        try:
            await server.run_db(_reject_agent_row, definition_id)
            yaml_content = await server.run_db(lambda: _get_manager().export_to_yaml(definition_id))
            return Response(
                content=yaml_content,
                media_type="application/x-yaml",
                headers={"Content-Disposition": f'attachment; filename="{definition_id}.yaml"'},
            )
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error exporting workflow definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/{definition_id}")
    async def get_workflow(definition_id: str) -> dict[str, Any]:
        """Get a workflow definition by ID."""
        try:
            manager = _get_manager()
            row = await server.run_db(manager.get, definition_id)
            _reject_agent_kind(row.workflow_type)
            return {"status": "success", "definition": row.to_dict()}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error getting workflow definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/import")
    async def import_workflow(request: ImportYAMLRequest) -> dict[str, Any]:
        """Import a workflow definition from YAML content."""
        try:
            parsed = yaml.safe_load(request.yaml_content)
            if isinstance(parsed, dict):
                _reject_agent_kind(parsed.get("type"))
            manager = _get_manager()
            row = await server.run_db(
                manager.import_from_yaml, request.yaml_content, project_id=request.project_id
            )
            await _broadcast_workflow("workflow_created", row.id)
            return {"status": "success", "definition": row.to_dict()}
        except HTTPException:
            raise
        except (ValueError, yaml.YAMLError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error importing workflow definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/duplicate")
    async def duplicate_workflow(definition_id: str, request: DuplicateRequest) -> dict[str, Any]:
        """Duplicate a workflow definition with a new name."""
        try:
            manager = _get_manager()
            await server.run_db(_reject_agent_row, definition_id)
            row = await server.run_db(manager.duplicate, definition_id, request.new_name)
            await _broadcast_workflow("workflow_created", row.id)
            return {"status": "success", "definition": row.to_dict()}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error duplicating workflow definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("")
    async def create_workflow(request: CreateWorkflowRequest) -> dict[str, Any]:
        """Create a new workflow definition."""
        try:
            definition = json.loads(request.definition_json)
            _reject_agent_kind(request.workflow_type)
            if request.workflow_type == "rule":
                RuleDefinitionBody.model_validate(definition)
        except (json.JSONDecodeError, ValidationError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid definition_json: {e}") from e

        try:
            manager = _get_manager()
            existing = await server.run_db(
                manager.get_by_name,
                request.name,
                project_id=request.project_id,
            )
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Workflow definition '{request.name}' already exists",
                )
            row = await server.run_db(
                manager.create,
                name=request.name,
                definition_json=request.definition_json,
                workflow_type=request.workflow_type,
                project_id=request.project_id,
                description=request.description,
                version=request.version,
                enabled=request.enabled,
                priority=request.priority,
                sources=request.sources,
                canvas_json=request.canvas_json,
                source=request.source,
                tags=(
                    None
                    if request.tags is None
                    else [tag for tag in request.tags if tag != "gobby"]
                ),
            )
            await _broadcast_workflow("workflow_created", row.id)
            return {"status": "success", "definition": row.to_dict()}
        except HTTPException:
            raise
        except UniqueViolation as e:
            raise HTTPException(
                status_code=409,
                detail=f"Workflow definition '{request.name}' already exists",
            ) from e
        except Exception as e:
            logger.exception("Error creating workflow definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/{definition_id}/toggle")
    async def toggle_workflow(definition_id: str) -> dict[str, Any]:
        """Toggle a workflow definition's enabled status."""
        try:
            manager = _get_manager()
            await server.run_db(_reject_agent_row, definition_id)
            updated = await server.run_db(manager.toggle_enabled, definition_id)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": updated.to_dict()}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error toggling workflow definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/{definition_id}")
    async def update_workflow(definition_id: str, request: UpdateWorkflowRequest) -> dict[str, Any]:
        """Update a workflow definition."""
        try:
            manager = _get_manager()
            await server.run_db(_reject_agent_row, definition_id)
            fields = request.model_dump(exclude_unset=True)
            if not fields:
                raise HTTPException(status_code=400, detail="No fields to update")
            row = await server.run_db(manager.update, definition_id, **fields)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Error updating workflow definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.delete("/{definition_id}")
    async def delete_workflow(definition_id: str) -> dict[str, Any]:
        """Delete a workflow definition (soft-delete)."""
        try:
            manager = _get_manager()
            await server.run_db(_reject_agent_row, definition_id)
            deleted = await server.run_db(manager.delete, definition_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Definition not found")
            await _broadcast_workflow("workflow_deleted", definition_id)
            return {"status": "success", "deleted": True}
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Error deleting workflow definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/restore-from-template")
    async def restore_from_template(definition_id: str) -> dict[str, Any]:
        """Restore an installed definition to match its bundled template."""
        try:
            from gobby.workflows.template_hashes import get_template_hash_cache

            manager = _get_manager()
            row = await server.run_db(manager.get, definition_id)
            _reject_agent_kind(row.workflow_type)
            cache = get_template_hash_cache()

            if not cache.has_drift(row):
                return {"status": "success", "message": "Definition already matches template"}

            # Re-read the template file and update the definition
            template_json = cache.get_template_json(row.workflow_type, row.name)
            if template_json is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No bundled template found for '{row.name}'",
                )

            updated = await server.run_db(manager.update, row.id, definition_json=template_json)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": updated.to_dict()}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error restoring from template: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/move-to-project")
    async def move_to_project(definition_id: str, request: MoveToProjectRequest) -> dict[str, Any]:
        """Move a definition to project scope."""
        try:
            manager = _get_manager()
            row = await server.run_db(manager.move_to_project, definition_id, request.project_id)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            msg = str(e)
            status = 400 if "template" in msg else 404
            raise HTTPException(status_code=status, detail=msg) from e
        except Exception as e:
            logger.exception("Error moving definition to project: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/move-to-global")
    async def move_to_global(definition_id: str) -> dict[str, Any]:
        """Move a definition to global (installed) scope."""
        try:
            manager = _get_manager()
            row = await server.run_db(manager.move_to_global, definition_id)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            msg = str(e)
            status = 400 if "template" in msg else 404
            raise HTTPException(status_code=status, detail=msg) from e
        except Exception as e:
            logger.exception("Error moving definition to global: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/restore")
    async def restore_workflow(definition_id: str) -> dict[str, Any]:
        """Restore a soft-deleted workflow definition."""
        try:
            manager = _get_manager()
            await server.run_db(_reject_agent_row, definition_id)
            row = await server.run_db(manager.restore, definition_id)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error restoring workflow definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    # --- Session Variables (top-level shortcuts) ---

    class SetVariableRequest(BaseModel):
        """Request body for setting a session variable."""

        name: str
        value: Any = None
        session_id: str
        scope: Literal["session", "step"] = "session"

    class GetVariableRequest(BaseModel):
        """Request body for getting session variable(s)."""

        name: str | None = None
        session_id: str
        scope: Literal["session", "step"] = "session"

    @router.post("/variables/set")
    async def set_variable(request: SetVariableRequest) -> dict[str, Any]:
        """Set a session-scoped variable."""
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        try:
            from gobby.mcp_proxy.tools.workflows._variables import set_variable as _set_var
            from gobby.workflows.step_instances import AgentStepInstanceManager

            instance_manager = (
                AgentStepInstanceManager(server.session_manager.db)
                if request.scope == "step"
                else None
            )

            return _set_var(
                server.session_manager,
                server.session_manager.db,
                name=request.name,
                value=request.value,
                session_id=request.session_id,
                scope=request.scope,
                instance_manager=instance_manager,
            )
        except Exception as e:
            logger.exception("Error setting variable: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/variables/get")
    async def get_variable(request: GetVariableRequest) -> dict[str, Any]:
        """Get session-scoped variable(s)."""
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        try:
            from gobby.mcp_proxy.tools.workflows._variables import get_variable as _get_var
            from gobby.workflows.step_instances import AgentStepInstanceManager

            instance_manager = (
                AgentStepInstanceManager(server.session_manager.db)
                if request.scope == "step"
                else None
            )

            return _get_var(
                server.session_manager,
                server.session_manager.db,
                name=request.name,
                session_id=request.session_id,
                scope=request.scope,
                instance_manager=instance_manager,
            )
        except Exception as e:
            logger.exception("Error getting variable: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    return router
