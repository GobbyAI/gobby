"""
Agent definition routes for Gobby HTTP server.

Provides endpoints for viewing and managing agent definitions
(merged from file-based and database sources).
"""

import logging
from typing import TYPE_CHECKING, Any

import yaml
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError, field_validator

from gobby.agents.reasoning import normalize_reasoning_effort

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class CreateAgentDefinitionRequest(BaseModel):
    """Request body for creating an agent definition in the DB."""

    name: str
    project_id: str | None = None
    description: str | None = None
    sources: list[str] | None = None
    surfaces: list[str] | None = None
    role: str | None = None
    goal: str | None = None
    personality: str | None = None
    instructions: str | None = None
    provider: str = "inherit"
    model: str | None = None
    reasoning_effort: str | None = None
    reasoning_required: bool | None = None
    fallback_agent: str | None = None
    mode: str = "inherit"
    isolation: str | None = "inherit"
    base_branch: str = "inherit"
    timeout: float = 0
    max_turns: int = 0
    default_workflow: str | None = None
    sandbox_config: dict[str, Any] | None = None
    workflows: dict[str, Any] | None = None
    lifecycle_variables: dict[str, Any] | None = None
    default_variables: dict[str, Any] | None = None
    blocked_tools: list[str] | None = None
    blocked_mcp_tools: list[str] | None = None

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_reasoning_effort(str(value))


class UpdateAgentDefinitionRequest(BaseModel):
    """Request body for updating an agent definition."""

    name: str | None = None
    description: str | None = None
    sources: list[str] | None = None
    surfaces: list[str] | None = None
    role: str | None = None
    goal: str | None = None
    personality: str | None = None
    instructions: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    reasoning_required: bool | None = None
    fallback_agent: str | None = None
    mode: str | None = None
    isolation: str | None = None
    base_branch: str | None = None
    timeout: float | None = None
    max_turns: int | None = None
    default_workflow: str | None = None
    sandbox_config: dict[str, Any] | None = None
    workflows: dict[str, Any] | None = None
    lifecycle_variables: dict[str, Any] | None = None
    default_variables: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None
    step_variables: dict[str, Any] | None = None
    exit_condition: str | None = None
    enabled: bool | None = None
    blocked_tools: list[str] | None = None
    blocked_mcp_tools: list[str] | None = None

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_reasoning_effort(str(value))


async def _batch_load_session_info(
    server: "HTTPServer", database: Any, session_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Load session token usage data for a batch of session IDs.

    Returns dict mapping session_id to enrichment fields.
    """
    import psycopg

    if not session_ids:
        return {}
    try:
        placeholders = ", ".join("%s" for _ in session_ids)

        def do_query() -> list[Any]:
            return list(
                database.fetchall(
                    f"""
                SELECT id, usage_input_tokens, usage_output_tokens,
                       usage_cache_creation_tokens, usage_cache_read_tokens,
                       summary_markdown, git_branch
                FROM sessions
                WHERE id IN ({placeholders})
                """,  # nosec B608
                    tuple(session_ids),
                )
            )

        rows = await server.run_db(do_query)
        result = {}
        for row in rows:
            result[row["id"]] = {
                "usage_input_tokens": row["usage_input_tokens"],
                "usage_output_tokens": row["usage_output_tokens"],
                "usage_cache_creation_tokens": row["usage_cache_creation_tokens"],
                "usage_cache_read_tokens": row["usage_cache_read_tokens"],
                "summary_markdown": row["summary_markdown"],
                "git_branch": row["git_branch"],
            }
        return result
    except psycopg.Error:
        logger.warning("Failed to load session info for agent runs", exc_info=True)
        return {}


def create_agents_router(server: "HTTPServer") -> APIRouter:
    """
    Create agents router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with agent definition endpoints
    """
    router = APIRouter(prefix="/api/agents", tags=["agents"])

    def _get_manager() -> Any:
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        return LocalWorkflowDefinitionManager(server.services.database)

    def _row_to_api_dict(row: Any) -> dict[str, Any] | None:
        """Convert a workflow_definitions DB row to API response dict.

        Returns None if the row fails Pydantic validation (logged and skipped).
        """
        from gobby.workflows.definitions import AgentDefinitionBody

        try:
            body = AgentDefinitionBody.model_validate_json(row.definition_json)
        except ValidationError as e:
            logger.warning(
                f"Skipping agent definition '{getattr(row, 'name', '?')}' "
                f"(id={getattr(row, 'id', '?')}): {e}"
            )
            return None
        from gobby.workflows.template_hashes import get_template_hash_cache

        cache = get_template_hash_cache()
        from gobby.llm.local_detection import is_local_agent_definition

        definition = body.model_dump(exclude_none=True)
        definition["is_local"] = is_local_agent_definition(body.provider, body.model)
        return {
            "definition": definition,
            "source": row.source,
            "enabled": row.enabled,
            "db_id": row.id,
            "deleted_at": row.deleted_at,
            "tags": row.tags,
            "sources": body.sources,
            "has_template_update": cache.has_drift(row),
        }

    @router.get("/definitions")
    async def list_definitions(
        project_id: str | None = Query(None),
        include_deleted: bool = Query(False),
        source_filter: str | None = Query(None),
        surface_filter: str | None = Query(None),
    ) -> dict[str, Any]:
        """List all agent definitions from workflow_definitions."""
        try:
            manager = _get_manager()
            rows = manager.list_all(
                workflow_type="agent",
                project_id=project_id,
                include_deleted=include_deleted,
            )
            items = [d for r in rows if (d := _row_to_api_dict(r)) is not None]
            if source_filter:
                items = [d for d in items if d.get("sources") and source_filter in d["sources"]]
            if surface_filter:
                items = [
                    d
                    for d in items
                    if surface_filter in d.get("definition", {}).get("surfaces", ["spawn"])
                ]
            return {
                "status": "success",
                "definitions": items,
                "count": len(items),
            }
        except Exception as e:
            logger.error(f"Error listing agent definitions: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/definitions/{name}/export")
    async def export_definition(
        name: str,
        project_id: str | None = Query(None),
    ) -> Response:
        """Export an agent definition as YAML for download."""
        try:
            from gobby.workflows.definitions import AgentDefinitionBody

            manager = _get_manager()
            rows = manager.list_all(workflow_type="agent", project_id=project_id)
            row = next((r for r in rows if r.name == name), None)
            if not row:
                raise HTTPException(status_code=404, detail=f"Agent definition '{name}' not found")

            body = AgentDefinitionBody.model_validate_json(row.definition_json)
            data = body.model_dump(exclude_none=True)
            yaml_content = yaml.dump(data, default_flow_style=False, sort_keys=False)

            return Response(
                content=yaml_content,
                media_type="application/x-yaml",
                headers={"Content-Disposition": f'attachment; filename="{name}.yaml"'},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error exporting agent definition '{name}': {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/definitions/{name}")
    async def get_definition(
        name: str,
        project_id: str | None = Query(None),
    ) -> dict[str, Any]:
        """Get a single agent definition by name."""
        try:
            manager = _get_manager()
            rows = manager.list_all(workflow_type="agent", project_id=project_id)
            row = next((r for r in rows if r.name == name), None)
            if not row:
                raise HTTPException(status_code=404, detail=f"Agent definition '{name}' not found")
            return {"status": "success", "definition": _row_to_api_dict(row)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting agent definition '{name}': {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/definitions")
    async def create_definition(
        request: CreateAgentDefinitionRequest,
    ) -> dict[str, Any]:
        """Create a new agent definition in the DB."""
        try:
            from gobby.workflows.definitions import AgentDefinitionBody, AgentWorkflows

            workflows = AgentWorkflows()
            if request.workflows:
                workflows = AgentWorkflows(**request.workflows)

            body = AgentDefinitionBody(
                name=request.name,
                description=request.description,
                sources=request.sources,
                surfaces=request.surfaces,
                role=request.role,
                goal=request.goal,
                personality=request.personality,
                instructions=request.instructions,
                provider=request.provider,
                model=request.model,
                reasoning_effort=request.reasoning_effort,
                reasoning_required=request.reasoning_required,
                fallback_agent=request.fallback_agent,
                mode=request.mode,
                isolation=request.isolation,
                base_branch=request.base_branch,
                timeout=request.timeout,
                max_turns=request.max_turns,
                workflows=workflows,
                blocked_tools=request.blocked_tools or [],
                blocked_mcp_tools=request.blocked_mcp_tools or [],
            )

            manager = _get_manager()
            row = manager.create(
                name=body.name,
                definition_json=body.model_dump_json(),
                workflow_type="agent",
                project_id=request.project_id,
                description=body.description,
                source="installed",
                enabled=body.enabled,
            )
            return {"status": "success", "definition": row.to_dict()}
        except Exception as e:
            logger.error(f"Error creating agent definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.put("/definitions/{definition_id}")
    async def update_definition(
        definition_id: str, request: UpdateAgentDefinitionRequest
    ) -> dict[str, Any]:
        """Update a DB-backed agent definition."""
        try:
            import json as _json

            manager = _get_manager()
            fields = request.model_dump(exclude_unset=True)
            if not fields:
                raise HTTPException(status_code=400, detail="No fields to update")

            # Load existing definition_json and apply updates
            row = manager.get(definition_id)
            body_dict: dict[str, Any] = _json.loads(row.definition_json)

            # Map body-level fields
            for key in (
                "name",
                "description",
                "sources",
                "surfaces",
                "role",
                "goal",
                "personality",
                "instructions",
                "provider",
                "model",
                "reasoning_effort",
                "reasoning_required",
                "fallback_agent",
                "mode",
                "isolation",
                "base_branch",
                "timeout",
                "max_turns",
                "default_workflow",
            ):
                if key in fields:
                    body_dict[key] = fields[key]

            # Nested dict fields that replace wholesale
            if "workflows" in fields:
                body_dict["workflows"] = fields["workflows"]
            if "sandbox_config" in fields:
                body_dict["sandbox"] = fields["sandbox_config"]
            if "lifecycle_variables" in fields:
                body_dict["lifecycle_variables"] = fields["lifecycle_variables"]
            if "default_variables" in fields:
                body_dict["default_variables"] = fields["default_variables"]
            for key in (
                "steps",
                "step_variables",
                "exit_condition",
                "blocked_tools",
                "blocked_mcp_tools",
            ):
                if key in fields:
                    body_dict[key] = fields[key]

            update_fields: dict[str, Any] = {
                "definition_json": _json.dumps(body_dict),
            }
            if "description" in fields:
                update_fields["description"] = fields["description"]
            if "enabled" in fields:
                update_fields["enabled"] = fields["enabled"]
            if "name" in fields:
                update_fields["name"] = fields["name"]

            row = manager.update(definition_id, **update_fields)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating agent definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/definitions/{definition_id}")
    async def delete_definition(definition_id: str) -> dict[str, Any]:
        """Delete a DB-backed agent definition (soft-delete)."""
        try:
            manager = _get_manager()
            deleted = manager.delete(definition_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Definition not found")
            return {"status": "success", "deleted": True}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting agent definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/definitions/{definition_id}/restore")
    async def restore_definition(definition_id: str) -> dict[str, Any]:
        """Restore a soft-deleted agent definition."""
        try:
            manager = _get_manager()
            row = manager.restore(definition_id)
            return {"status": "success", "definition": row.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error restoring agent definition: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # -------------------------------------------------------------------------
    # Rules and variables PATCH endpoints
    # -------------------------------------------------------------------------

    class PatchRulesRequest(BaseModel):
        """Request body for patching agent rules."""

        add: list[str] | None = None
        remove: list[str] | None = None

    class PatchRuleSelectorsRequest(BaseModel):
        """Request body for patching agent rule selectors."""

        add_include: list[str] | None = None
        remove_include: list[str] | None = None
        add_exclude: list[str] | None = None
        remove_exclude: list[str] | None = None

    class PatchVariablesRequest(BaseModel):
        """Request body for patching agent variables."""

        set: dict[str, Any] | None = None
        remove: list[str] | None = None

    @router.patch("/definitions/{definition_id}/rules")
    async def patch_rules(definition_id: str, request: PatchRulesRequest) -> dict[str, Any]:
        """Add or remove rules from an agent definition."""
        try:
            import json as _json

            manager = _get_manager()
            row = manager.get(definition_id)
            body_dict: dict[str, Any] = _json.loads(row.definition_json)

            workflows = body_dict.get("workflows", {})
            rules: list[str] = list(workflows.get("rules", []))

            if request.remove:
                rules = [r for r in rules if r not in request.remove]
            if request.add:
                for rule in request.add:
                    if rule not in rules:
                        rules.append(rule)

            workflows["rules"] = rules
            body_dict["workflows"] = workflows
            manager.update(definition_id, definition_json=_json.dumps(body_dict))

            return {"status": "success", "rules": rules}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error patching rules: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.patch("/definitions/{definition_id}/rule-selectors")
    async def patch_rule_selectors(
        definition_id: str, request: PatchRuleSelectorsRequest
    ) -> dict[str, Any]:
        """Add or remove rule selectors from an agent definition."""
        try:
            import json as _json

            manager = _get_manager()
            row = manager.get(definition_id)
            body_dict: dict[str, Any] = _json.loads(row.definition_json)

            workflows = body_dict.get("workflows", {})
            selectors = workflows.get("rule_selectors") or {"include": [], "exclude": []}
            include: list[str] = list(selectors.get("include", []))
            exclude: list[str] = list(selectors.get("exclude", []))

            if request.remove_include:
                include = [s for s in include if s not in request.remove_include]
            if request.add_include:
                for s in request.add_include:
                    if s not in include:
                        include.append(s)
            if request.remove_exclude:
                exclude = [s for s in exclude if s not in request.remove_exclude]
            if request.add_exclude:
                for s in request.add_exclude:
                    if s not in exclude:
                        exclude.append(s)

            rule_selectors = {"include": include, "exclude": exclude}
            workflows["rule_selectors"] = rule_selectors
            body_dict["workflows"] = workflows
            manager.update(definition_id, definition_json=_json.dumps(body_dict))

            return {"status": "success", "rule_selectors": rule_selectors}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error patching rule selectors: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.patch("/definitions/{definition_id}/variables")
    async def patch_variables(definition_id: str, request: PatchVariablesRequest) -> dict[str, Any]:
        """Set or remove variables from an agent definition."""
        try:
            import json as _json

            manager = _get_manager()
            row = manager.get(definition_id)
            body_dict: dict[str, Any] = _json.loads(row.definition_json)

            workflows = body_dict.get("workflows", {})
            variables: dict[str, Any] = dict(workflows.get("variables", {}))

            if request.remove:
                for key in request.remove:
                    variables.pop(key, None)
            if request.set:
                variables.update(request.set)

            workflows["variables"] = variables
            body_dict["workflows"] = workflows
            manager.update(definition_id, definition_json=_json.dumps(body_dict))

            return {"status": "success", "variables": variables}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error patching variables: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # -------------------------------------------------------------------------
    # Running agents and agent runs
    # -------------------------------------------------------------------------

    @router.get("/running")
    async def list_running_agents() -> dict[str, Any]:
        """List all currently running agents from the database."""
        try:
            from gobby.storage.agents import LocalAgentRunManager

            manager = LocalAgentRunManager(server.services.database)
            runs = manager.list_active()
            return {
                "status": "success",
                "agents": [r.to_dict() for r in runs],
                "count": len(runs),
            }
        except Exception as e:
            logger.error(f"Error listing running agents: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/runs")
    async def list_agent_runs(
        status: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        project_id: str | None = Query(
            None, description="Filter by project ID (via parent session)"
        ),
    ) -> dict[str, Any]:
        """List recent agent runs from the database with session enrichment."""
        try:
            from gobby.storage.agents import LocalAgentRunManager

            manager = LocalAgentRunManager(server.services.database)
            runs = manager.list_by_status(status=status, limit=limit, project_id=project_id)

            # Enrich with session data (token usage, cost)
            enriched = []
            session_ids = [r.child_session_id for r in runs if r.child_session_id]
            session_map = await _batch_load_session_info(
                server, server.services.database, session_ids
            )

            for r in runs:
                d = r.to_dict()
                if r.child_session_id and r.child_session_id in session_map:
                    d.update(session_map[r.child_session_id])
                enriched.append(d)

            return {
                "status": "success",
                "runs": enriched,
                "count": len(enriched),
            }
        except Exception as e:
            logger.error(f"Error listing agent runs: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/runs/{run_id}")
    async def get_agent_run_detail(run_id: str) -> dict[str, Any]:
        """Get detailed agent run info with session enrichment."""
        try:
            from gobby.storage.agents import LocalAgentRunManager

            manager = LocalAgentRunManager(server.services.database)
            run = manager.get(run_id)
            if not run:
                raise HTTPException(status_code=404, detail=f"Agent run '{run_id}' not found")

            d = run.to_dict()

            # Session enrichment
            if run.child_session_id:
                session_info = await _batch_load_session_info(
                    server, server.services.database, [run.child_session_id]
                )
                if run.child_session_id in session_info:
                    d.update(session_info[run.child_session_id])

            return {"status": "success", "run": d}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting agent run detail: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/runs/{run_id}/cancel")
    async def cancel_agent_run(run_id: str) -> dict[str, Any]:
        """Cancel a running agent."""
        try:
            from gobby.agents.kill import kill_agent
            from gobby.storage.agents import LocalAgentRunManager

            db = server.services.database
            manager = LocalAgentRunManager(db)
            run = manager.get(run_id)
            if not run:
                raise HTTPException(status_code=404, detail=f"Agent run '{run_id}' not found")
            if run.status not in ("running", "pending"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Agent run '{run_id}' is not active (status={run.status})",
                )

            result = await kill_agent(run, db, signal_name="TERM")

            # Update DB status
            manager.cancel(run_id)

            return {"status": "success", "result": result}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error cancelling agent run '{run_id}': {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/definitions/{name}/install")
    async def install_definition_from_template(
        name: str,
    ) -> dict[str, Any]:
        """Legacy endpoint — template rows no longer exist.

        Definitions are now installed directly by sync functions.
        """
        raise HTTPException(
            status_code=410,
            detail="Template installation is no longer needed. Definitions are installed directly during sync.",
        )

    @router.post("/definitions/import/{name}")
    async def import_definition(
        name: str,
        project_id: str | None = Query(None),
    ) -> dict[str, Any]:
        """Copy a file-based agent definition into the DB for customization."""
        try:
            from gobby.agents.sync import get_bundled_agents_path
            from gobby.workflows.definitions import AgentDefinitionBody

            # Load from bundled agents directory
            agents_path = get_bundled_agents_path()
            yaml_path = agents_path / f"{name}.yaml"
            if not yaml_path.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"File-based agent definition '{name}' not found",
                )
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            data["name"] = data.get("name", name)
            body = AgentDefinitionBody.model_validate(data)
            manager = _get_manager()
            row = manager.create(
                name=body.name,
                definition_json=body.model_dump_json(),
                workflow_type="agent",
                project_id=project_id,
                description=body.description,
                source="installed",
                enabled=body.enabled,
            )
            return {"status": "success", "definition": row.to_dict()}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error importing agent definition '{name}': {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
