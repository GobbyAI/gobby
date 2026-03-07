"""
Workflow definition routes for Gobby HTTP server.

Provides CRUD endpoints for managing workflow definitions in the database.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from gobby.utils.metrics import get_metrics_collector

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
    source: str = "installed"
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
    metrics = get_metrics_collector()

    def _get_manager() -> "LocalWorkflowDefinitionManager":
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        return LocalWorkflowDefinitionManager(server.services.database)

    async def _broadcast_workflow(event: str, definition_id: str, **kwargs: Any) -> None:
        """Broadcast a workflow event via WebSocket if available."""
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_workflow_event(event, definition_id, **kwargs)
            except Exception as e:
                logger.debug(f"Failed to broadcast workflow event {event}: {e}")

    @router.get("/templates")
    async def list_templates() -> dict[str, Any]:
        """List available workflow templates for the 'New' button."""
        from gobby.workflows.workflow_templates import get_workflow_templates

        metrics.inc_counter("http_requests_total")
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
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            rows = manager.list_all(
                project_id=project_id,
                workflow_type=workflow_type,
                enabled=enabled,
                include_deleted=include_deleted,
            )
            return {
                "status": "success",
                "definitions": [r.to_dict() for r in rows],
                "count": len(rows),
            }
        except Exception as e:
            logger.exception("Error listing workflow definitions")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/{definition_id}/export")
    async def export_workflow(definition_id: str) -> Response:
        """Export a workflow definition as YAML."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            yaml_content = manager.export_to_yaml(definition_id)
            return Response(
                content=yaml_content,
                media_type="application/x-yaml",
                headers={"Content-Disposition": f'attachment; filename="{definition_id}.yaml"'},
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error exporting workflow definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/{definition_id}")
    async def get_workflow(definition_id: str) -> dict[str, Any]:
        """Get a workflow definition by ID."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            row = manager.get(definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error getting workflow definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/import")
    async def import_workflow(request: ImportYAMLRequest) -> dict[str, Any]:
        """Import a workflow definition from YAML content."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            row = manager.import_from_yaml(request.yaml_content, project_id=request.project_id)
            await _broadcast_workflow("workflow_created", row.id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error importing workflow definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{definition_id}/duplicate")
    async def duplicate_workflow(definition_id: str, request: DuplicateRequest) -> dict[str, Any]:
        """Duplicate a workflow definition with a new name."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            row = manager.duplicate(definition_id, request.new_name)
            await _broadcast_workflow("workflow_created", row.id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error duplicating workflow definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("")
    async def create_workflow(request: CreateWorkflowRequest) -> dict[str, Any]:
        """Create a new workflow definition."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            row = manager.create(
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
                tags=request.tags,
            )
            await _broadcast_workflow("workflow_created", row.id)
            return {"status": "success", "definition": row.to_dict()}
        except Exception as e:
            logger.error(f"Error creating workflow definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.put("/{definition_id}/toggle")
    async def toggle_workflow(definition_id: str) -> dict[str, Any]:
        """Toggle a workflow definition's enabled status."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            row = manager.get(definition_id)
            updated = manager.update(definition_id, enabled=not row.enabled)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": updated.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error toggling workflow definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.put("/{definition_id}")
    async def update_workflow(definition_id: str, request: UpdateWorkflowRequest) -> dict[str, Any]:
        """Update a workflow definition."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            fields = request.model_dump(exclude_unset=True)
            if not fields:
                raise HTTPException(status_code=400, detail="No fields to update")
            row = manager.update(definition_id, **fields)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating workflow definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/{definition_id}")
    async def delete_workflow(definition_id: str) -> dict[str, Any]:
        """Delete a workflow definition (soft-delete)."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            deleted = manager.delete(definition_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Definition not found")
            await _broadcast_workflow("workflow_deleted", definition_id)
            return {"status": "success", "deleted": True}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting workflow definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{definition_id}/install")
    async def install_from_template(definition_id: str) -> dict[str, Any]:
        """Create a custom copy from a bundled definition."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            row = manager.install_from_template(definition_id)
            await _broadcast_workflow("workflow_created", row.id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error installing from template: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/install-all-templates")
    async def install_all_templates(
        workflow_type: str | None = Query(None),
    ) -> dict[str, Any]:
        """Create custom copies of all eligible bundled definitions."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            rows = manager.install_all_templates(workflow_type=workflow_type)
            if rows:
                names = [r.name for r in rows]
                await _broadcast_workflow(
                    "workflows_bulk_changed",
                    "bulk",
                    count=len(rows),
                    names=names[:10],
                )
            return {
                "status": "success",
                "definitions": [r.to_dict() for r in rows],
                "count": len(rows),
            }
        except Exception as e:
            logger.error(f"Error installing all templates: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{definition_id}/move-to-project")
    async def move_to_project(definition_id: str, request: MoveToProjectRequest) -> dict[str, Any]:
        """Move a definition to project scope."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            row = manager.move_to_project(definition_id, request.project_id)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            msg = str(e)
            status = 400 if "template" in msg else 404
            raise HTTPException(status_code=status, detail=msg) from e
        except Exception as e:
            logger.error(f"Error moving definition to project: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{definition_id}/move-to-global")
    async def move_to_global(definition_id: str) -> dict[str, Any]:
        """Move a definition to global (installed) scope."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            row = manager.move_to_global(definition_id)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            msg = str(e)
            status = 400 if "template" in msg else 404
            raise HTTPException(status_code=status, detail=msg) from e
        except Exception as e:
            logger.error(f"Error moving definition to global: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{definition_id}/restore")
    async def restore_workflow(definition_id: str) -> dict[str, Any]:
        """Restore a soft-deleted workflow definition."""
        metrics.inc_counter("http_requests_total")
        try:
            manager = _get_manager()
            row = manager.restore(definition_id)
            await _broadcast_workflow("workflow_updated", definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error restoring workflow definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # --- Session Variables (top-level shortcuts) ---

    class SetVariableRequest(BaseModel):
        """Request body for setting a session variable."""

        name: str
        value: str | int | float | bool | None = None
        session_id: str | None = None

    class GetVariableRequest(BaseModel):
        """Request body for getting session variable(s)."""

        name: str | None = None
        session_id: str | None = None

    @router.post("/variables/set")
    async def set_variable(request: SetVariableRequest) -> dict[str, Any]:
        """Set a session-scoped variable."""
        metrics.inc_counter("http_requests_total")
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        try:
            from gobby.mcp_proxy.tools.workflows._variables import set_variable as _set_var

            return _set_var(
                server.session_manager,
                server.session_manager.db,
                name=request.name,
                value=request.value,
                session_id=request.session_id,
                workflow=None,
            )
        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error setting variable: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/variables/get")
    async def get_variable(request: GetVariableRequest) -> dict[str, Any]:
        """Get session-scoped variable(s)."""
        metrics.inc_counter("http_requests_total")
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        try:
            from gobby.mcp_proxy.tools.workflows._variables import get_variable as _get_var

            return _get_var(
                server.session_manager,
                server.session_manager.db,
                name=request.name,
                session_id=request.session_id,
                workflow=None,
            )
        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error getting variable: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
