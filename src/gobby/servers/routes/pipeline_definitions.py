"""Pipeline definition routes for Gobby HTTP server.

Mounted at /api/pipelines/definitions and registered before the execution
router so GET /{execution_id} cannot shadow /definitions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import yaml
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from gobby.storage.definitions._shared import (
    DefinitionNameConflictError,
    DefinitionNotFoundError,
)
from gobby.storage.definitions.pipelines import (
    PipelineDefinitionManager,
    PipelineDefinitionRow,
)
from gobby.workflows.pipeline_models import PipelineDefinition

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class CreatePipelineRequest(BaseModel):
    """Request body for creating a pipeline definition."""

    name: str
    definition_json: str
    project_id: str | None = None
    description: str | None = None
    version: str = "1.0"
    enabled: bool = True
    canvas_json: str | None = None
    source: Literal["installed", "custom", "project"] = "custom"
    tags: list[str] | None = None


class UpdatePipelineRequest(BaseModel):
    """Request body for updating a pipeline definition."""

    name: str | None = None
    definition_json: str | None = None
    description: str | None = None
    version: str | None = None
    enabled: bool | None = None
    canvas_json: str | None = None
    tags: list[str] | None = None


class ImportYAMLRequest(BaseModel):
    """Request body for importing a pipeline from YAML."""

    yaml_content: str
    project_id: str | None = None


class MoveToProjectRequest(BaseModel):
    """Request body for moving a pipeline to project scope."""

    project_id: str


class DuplicateRequest(BaseModel):
    """Request body for duplicating a pipeline."""

    new_name: str


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dumps(payload: Any) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    return json.dumps(payload)


def _row_to_dict(row: PipelineDefinitionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "kind": "pipeline",
        "description": row.description,
        "version": row.version,
        "enabled": row.enabled,
        "source": row.source,
        "tags": row.tags,
        "project_id": row.project_id,
        "definition_json": _dumps(row.definition_json),
        "canvas_json": _dumps(row.canvas_json),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _pipeline_templates() -> list[dict[str, Any]]:
    blank = {
        "name": "",
        "type": "pipeline",
        "description": "",
        "version": "1.0",
        "steps": [{"id": "step-1", "exec": "echo 'hello world'"}],
    }
    ci = {
        "name": "",
        "type": "pipeline",
        "description": "CI pipeline with build, test, and deploy stages",
        "version": "1.0",
        "steps": [
            {"id": "build", "exec": "echo 'Building...'"},
            {"id": "test", "exec": "echo 'Running tests...'", "condition": "steps.build.success"},
            {
                "id": "approval",
                "exec": "echo 'Approved - deploying'",
                "approval": {"required": True, "message": "Deploy to production?"},
            },
            {"id": "deploy", "exec": "echo 'Deploying...'"},
        ],
    }
    return [
        {
            "id": "blank-pipeline",
            "name": "Blank Pipeline",
            "description": "Empty sequential pipeline with one step",
            "kind": "pipeline",
            "definition_json": json.dumps(blank),
        },
        {
            "id": "ci-pipeline",
            "name": "CI Pipeline Template",
            "description": "Build/test/deploy pipeline with approval gate",
            "kind": "pipeline",
            "definition_json": json.dumps(ci),
        },
    ]


def _parse_definition(raw: str | dict[str, Any]) -> dict[str, Any]:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError("definition_json must be a JSON object")
    PipelineDefinition.model_validate(data)
    return data


def create_pipeline_definitions_router(server: HTTPServer) -> APIRouter:
    """Create pipeline-definition router bound to the server instance."""
    router = APIRouter(prefix="/api/pipelines/definitions", tags=["pipeline-definitions"])

    def _get_manager() -> PipelineDefinitionManager:
        return PipelineDefinitionManager(server.services.database)

    async def _broadcast(event: str, definition_id: str, **kwargs: Any) -> None:
        ws = server.services.websocket_server
        if not ws:
            return
        try:
            await ws.broadcast_workflow_event(event, definition_id, **kwargs)
        except Exception as e:
            logger.debug("Failed to broadcast pipeline event %s: %s", event, e)

    @router.get("/templates")
    async def list_templates() -> dict[str, Any]:
        templates = _pipeline_templates()
        return {"status": "success", "templates": templates, "count": len(templates)}

    @router.get("")
    async def list_definitions(
        enabled: bool | None = Query(None),
        project_id: str | None = Query(None),
        include_deleted: bool = Query(False),
    ) -> dict[str, Any]:
        try:
            rows = await server.run_db(
                _get_manager().list_all,
                project_id=project_id,
                enabled=enabled,
                include_deleted=include_deleted,
            )
            definitions = [_row_to_dict(row) for row in rows]
            from gobby.workflows.template_hashes import get_template_hash_cache

            get_template_hash_cache().annotate_rows(definitions)
            return {
                "status": "success",
                "definitions": definitions,
                "count": len(definitions),
            }
        except Exception as e:
            logger.exception("Error listing pipeline definitions")
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/{definition_id}/export")
    async def export_definition(definition_id: str) -> Response:
        try:
            row = await server.run_db(_get_manager().get, definition_id)
            payload = dict(row.definition_json)
            payload["name"] = row.name
            yaml_content = yaml.dump(payload, default_flow_style=False, sort_keys=False)
            return Response(
                content=yaml_content,
                media_type="application/x-yaml",
                headers={"Content-Disposition": f'attachment; filename="{row.name}.yaml"'},
            )
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error exporting pipeline definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/{definition_id}")
    async def get_definition(definition_id: str) -> dict[str, Any]:
        try:
            row = await server.run_db(_get_manager().get, definition_id)
            return {"status": "success", "definition": _row_to_dict(row)}
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error getting pipeline definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/import")
    async def import_definition(request: ImportYAMLRequest) -> dict[str, Any]:
        try:
            parsed = yaml.safe_load(request.yaml_content)
            if not isinstance(parsed, dict) or "name" not in parsed:
                raise ValueError("Invalid YAML: must be a mapping with a 'name' field")
            if parsed.get("type") != "pipeline":
                raise ValueError("YAML must have 'type: pipeline'")
            data = _parse_definition(parsed)
            manager = _get_manager()
            row = await server.run_db(
                manager.create,
                name=str(data["name"]),
                definition_json=data,
                project_id=request.project_id,
                description=data.get("description"),
                version=str(data.get("version", "1.0")),
                enabled=bool(data.get("enabled", True)),
                source="custom",
            )
            await _broadcast("pipeline_created", row.id)
            return {"status": "success", "definition": _row_to_dict(row)}
        except HTTPException:
            raise
        except (ValueError, yaml.YAMLError, ValidationError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except DefinitionNameConflictError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error importing pipeline definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/duplicate")
    async def duplicate_definition(definition_id: str, request: DuplicateRequest) -> dict[str, Any]:
        try:
            row = await server.run_db(_get_manager().duplicate, definition_id, request.new_name)
            await _broadcast("pipeline_created", row.id)
            return {"status": "success", "definition": _row_to_dict(row)}
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except DefinitionNameConflictError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error duplicating pipeline definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("")
    async def create_definition(request: CreatePipelineRequest) -> dict[str, Any]:
        try:
            data = _parse_definition(request.definition_json)
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid definition_json: {e}") from e
        try:
            manager = _get_manager()
            canvas = None if request.canvas_json is None else json.loads(request.canvas_json)
            row = await server.run_db(
                manager.create,
                name=request.name,
                definition_json=data,
                project_id=request.project_id,
                description=request.description,
                version=request.version,
                enabled=request.enabled,
                canvas_json=canvas,
                source=request.source,
                tags=(
                    None
                    if request.tags is None
                    else [tag for tag in request.tags if tag != "gobby"]
                ),
            )
            await _broadcast("pipeline_created", row.id)
            return {"status": "success", "definition": _row_to_dict(row)}
        except HTTPException:
            raise
        except DefinitionNameConflictError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error creating pipeline definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/{definition_id}/toggle")
    async def toggle_definition(definition_id: str) -> dict[str, Any]:
        try:
            updated = await server.run_db(_get_manager().toggle_enabled, definition_id)
            await _broadcast("pipeline_updated", definition_id)
            return {"status": "success", "definition": _row_to_dict(updated)}
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error toggling pipeline definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/{definition_id}")
    async def update_definition(
        definition_id: str, request: UpdatePipelineRequest
    ) -> dict[str, Any]:
        try:
            fields = request.model_dump(exclude_unset=True)
            if not fields:
                raise HTTPException(status_code=400, detail="No fields to update")
            if "definition_json" in fields:
                fields["definition_json"] = _parse_definition(fields["definition_json"])
            if "canvas_json" in fields and isinstance(fields["canvas_json"], str):
                fields["canvas_json"] = json.loads(fields["canvas_json"])
            if "tags" in fields and fields["tags"] is not None:
                fields["tags"] = [tag for tag in fields["tags"] if tag != "gobby"]
            row = await server.run_db(_get_manager().update, definition_id, **fields)
            await _broadcast("pipeline_updated", definition_id)
            return {"status": "success", "definition": _row_to_dict(row)}
        except HTTPException:
            raise
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error updating pipeline definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.delete("/{definition_id}")
    async def delete_definition(definition_id: str) -> dict[str, Any]:
        try:
            deleted = await server.run_db(_get_manager().delete, definition_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Definition not found")
            await _broadcast("pipeline_deleted", definition_id)
            return {"status": "success", "deleted": True}
        except HTTPException:
            raise
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error deleting pipeline definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/restore-from-template")
    async def restore_from_template(definition_id: str) -> dict[str, Any]:
        try:
            from gobby.workflows.template_hashes import get_template_hash_cache

            manager = _get_manager()
            row = await server.run_db(manager.get, definition_id)
            cache = get_template_hash_cache()
            if not cache.has_drift(row, kind="pipeline"):
                return {"status": "success", "message": "Definition already matches template"}
            template_json = cache.get_template_json("pipeline", row.name)
            if template_json is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No bundled template found for '{row.name}'",
                )
            updated = await server.run_db(
                manager.update, row.id, definition_json=json.loads(template_json)
            )
            await _broadcast("pipeline_updated", definition_id)
            return {"status": "success", "definition": _row_to_dict(updated)}
        except HTTPException:
            raise
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error restoring pipeline from template: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/move-to-project")
    async def move_to_project(definition_id: str, request: MoveToProjectRequest) -> dict[str, Any]:
        try:
            row = await server.run_db(
                _get_manager().move_to_project, definition_id, request.project_id
            )
            await _broadcast("pipeline_updated", definition_id)
            return {"status": "success", "definition": _row_to_dict(row)}
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            msg = str(e)
            status = 400 if "template" in msg else 404
            raise HTTPException(status_code=status, detail=msg) from e
        except Exception as e:
            logger.exception("Error moving pipeline to project: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/move-to-global")
    async def move_to_global(definition_id: str) -> dict[str, Any]:
        try:
            row = await server.run_db(_get_manager().move_to_global, definition_id)
            await _broadcast("pipeline_updated", definition_id)
            return {"status": "success", "definition": _row_to_dict(row)}
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            msg = str(e)
            status = 400 if "template" in msg else 404
            raise HTTPException(status_code=status, detail=msg) from e
        except Exception as e:
            logger.exception("Error moving pipeline to global: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{definition_id}/restore")
    async def restore_definition(definition_id: str) -> dict[str, Any]:
        try:
            row = await server.run_db(_get_manager().restore, definition_id)
            await _broadcast("pipeline_updated", definition_id)
            return {"status": "success", "definition": _row_to_dict(row)}
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error restoring pipeline definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    return router
