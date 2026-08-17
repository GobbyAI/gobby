"""Variable default definition routes for Gobby HTTP server."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ValidationError

from gobby.mcp_proxy.tools.workflows._variables import _variable_summary
from gobby.storage.definitions._shared import (
    DefinitionNameConflictError,
    DefinitionNotFoundError,
)
from gobby.storage.definitions.variables import (
    SessionVariableDefaultManager,
    SessionVariableDefaultRow,
)
from gobby.workflows.definitions import VariableDefinitionBody

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class CreateVariableRequest(BaseModel):
    """Request body for creating a session variable default."""

    name: str
    value: Any = None
    description: str | None = None
    project_id: str | None = None
    enabled: bool = True
    tags: list[str] | None = None


class UpdateVariableRequest(BaseModel):
    """Request body for updating a session variable default."""

    value: Any = None
    description: str | None = None
    enabled: bool | None = None
    tags: list[str] | None = None


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _variable_dict(row: SessionVariableDefaultRow) -> dict[str, Any]:
    summary = _variable_summary(row)
    summary["kind"] = "variable"
    summary["created_at"] = _iso(row.created_at)
    summary["updated_at"] = _iso(row.updated_at)
    summary["deleted_at"] = _iso(row.deleted_at)
    summary["definition_json"] = json.dumps(
        {
            "variable": row.name,
            "value": row.default_value,
            "description": row.description,
        },
        sort_keys=True,
    )
    return summary


def create_variable_definitions_router(server: HTTPServer) -> APIRouter:
    """Create /api/variables router bound to the server instance."""
    router = APIRouter(prefix="/api/variables", tags=["variables"])

    def _get_manager() -> SessionVariableDefaultManager:
        return SessionVariableDefaultManager(server.services.database)

    def _resolve(manager: SessionVariableDefaultManager, ref: str) -> SessionVariableDefaultRow:
        try:
            return manager.get(ref)
        except DefinitionNotFoundError:
            row = manager.get_by_name(ref)
            if row is None:
                raise DefinitionNotFoundError(f"Session variable default {ref} not found") from None
            return row

    @router.get("")
    async def list_variables(
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
            variables = [_variable_dict(row) for row in rows]
            from gobby.workflows.template_hashes import get_template_hash_cache

            get_template_hash_cache().annotate_rows(variables)
            return {"status": "success", "variables": variables, "count": len(variables)}
        except Exception as e:
            logger.exception("Error listing variable definitions")
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("")
    async def create_variable(request: CreateVariableRequest) -> dict[str, Any]:
        try:
            VariableDefinitionBody(
                variable=request.name, value=request.value, description=request.description
            )
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=f"Invalid variable: {e}") from e
        try:
            manager = _get_manager()
            row = await server.run_db(
                manager.create,
                name=request.name,
                default_value=request.value,
                project_id=request.project_id,
                description=request.description,
                enabled=request.enabled,
                tags=request.tags,
                source="custom",
            )
            return {"status": "success", "variable": _variable_dict(row)}
        except HTTPException:
            raise
        except DefinitionNameConflictError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error creating variable definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/{ref}/toggle")
    async def toggle_variable(ref: str) -> dict[str, Any]:
        try:
            manager = _get_manager()
            row = await server.run_db(_resolve, manager, ref)
            updated = await server.run_db(manager.toggle_enabled, row.id)
            return {"status": "success", "variable": _variable_dict(updated)}
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error toggling variable definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.put("/{ref}")
    async def update_variable(ref: str, request: UpdateVariableRequest) -> dict[str, Any]:
        try:
            manager = _get_manager()
            row = await server.run_db(_resolve, manager, ref)
            fields = request.model_dump(exclude_unset=True)
            if "value" in fields:
                fields["default_value"] = fields.pop("value")
            if not fields:
                raise HTTPException(status_code=400, detail="No fields to update")
            updated = await server.run_db(manager.update, row.id, **fields)
            return {"status": "success", "variable": _variable_dict(updated)}
        except HTTPException:
            raise
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error updating variable definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.delete("/{ref}")
    async def delete_variable(ref: str) -> dict[str, Any]:
        try:
            manager = _get_manager()
            row = await server.run_db(_resolve, manager, ref)
            deleted = await server.run_db(manager.delete, row.id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Definition not found")
            return {"status": "success", "deleted": True}
        except HTTPException:
            raise
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error deleting variable definition: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{ref}/restore-from-template")
    async def restore_from_template(ref: str) -> dict[str, Any]:
        try:
            from gobby.workflows.template_hashes import get_template_hash_cache

            manager = _get_manager()
            row = await server.run_db(_resolve, manager, ref)
            cache = get_template_hash_cache()
            template_json = cache.get_template_json("variable", row.name)
            if template_json is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No bundled template found for '{row.name}'",
                )
            payload = json.loads(template_json)
            updated = await server.run_db(
                manager.update,
                row.id,
                default_value=payload.get("value"),
                description=payload.get("description"),
            )
            return {"status": "success", "variable": _variable_dict(updated)}
        except HTTPException:
            raise
        except DefinitionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error restoring variable from template: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    return router
