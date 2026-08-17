"""
Pipeline tool registration for the unified gobby-workflows server.

Contains helpers and a register_pipeline_tools() function that adds all
pipeline-related MCP tools to a given InternalToolRegistry.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import yaml
from pydantic import ValidationError

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.workflows._pipeline_discovery import list_pipelines
from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    approve_pipeline,
    cancel_pipeline,
    get_pipeline_status,
    reject_pipeline,
    resume_pipeline,
    run_pipeline,
)
from gobby.mcp_proxy.tools.workflows._pipeline_query import (
    clear_pipeline_execution_history,
    list_pipeline_executions,
    search_pipeline_executions,
)
from gobby.storage.definitions.pipelines import (
    PipelineDefinitionManager,
    PipelineDefinitionRow,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.definitions import normalize_workflow_definition_enabled
from gobby.workflows.pipeline_models import PipelineDefinition

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def _export_row(row: PipelineDefinitionRow) -> SimpleNamespace:
    return SimpleNamespace(
        name=row.name,
        definition_json=json.dumps(row.definition_json),
        tags=row.tags,
    )


def _resolve_pipeline(
    def_manager: PipelineDefinitionManager,
    name: str | None = None,
    definition_id: str | None = None,
    include_deleted: bool = False,
) -> PipelineDefinitionRow:
    if definition_id:
        return def_manager.get(definition_id, include_deleted=include_deleted)
    if name:
        row = def_manager.get_by_name(name, include_deleted=include_deleted)
        if row is None:
            raise ValueError(f"Pipeline '{name}' not found")
        return row
    raise ValueError("Either 'name' or 'definition_id' is required")


def _require_pipeline(
    def_manager: PipelineDefinitionManager,
    name: str | None = None,
    definition_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a typed pipeline row. Returns error dict or None."""
    try:
        _resolve_pipeline(def_manager, name, definition_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return None


def _parse_pipeline_yaml(yaml_content: str) -> dict[str, Any]:
    data = yaml.safe_load(yaml_content)
    if not isinstance(data, dict) or "name" not in data:
        raise ValueError("Invalid YAML: must be a mapping with a 'name' field")
    if data.get("type") != "pipeline":
        raise ValueError("YAML must have 'type: pipeline'")
    PipelineDefinition(**data)
    return data


def _pipeline_summary(row: PipelineDefinitionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "version": row.version,
        "enabled": row.enabled,
        "source": row.source,
        "tags": row.tags,
        "project_id": row.project_id,
    }


def create_pipeline_definition(
    def_manager: PipelineDefinitionManager,
    loader: Any,
    yaml_content: str,
    project_id: str | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    try:
        data = _parse_pipeline_yaml(yaml_content)
    except yaml.YAMLError as e:
        return {"success": False, "error": f"YAML parse error: {e}"}
    except (ValueError, TypeError, ValidationError) as e:
        return {"success": False, "error": f"Validation failed: {e}"}

    name = str(data["name"])
    existing = def_manager.get_by_name(name, project_id=project_id)
    if existing:
        return {
            "success": False,
            "error": (
                f"Definition '{name}' already exists (id={existing.id}). "
                "Use update_pipeline to modify it."
            ),
        }

    try:
        row = def_manager.create(
            name=name,
            definition_json=data,
            project_id=project_id,
            description=data.get("description", ""),
            version=str(data.get("version", "1.0")),
            enabled=normalize_workflow_definition_enabled(data),
            source="custom",
            tags=["user"],
        )
    except Exception as e:
        return {"success": False, "error": f"Import failed: {e}"}

    loader.clear_cache()
    logger.info("Created pipeline definition '%s' (id=%s)", row.name, row.id)
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(
            cast(Any, _export_row(row)),
            project_path,
            kind="pipeline",
            make_global=make_global_template,
        )
    except Exception as e:
        logger.warning("Failed to auto-export definition '%s': %s", row.name, e)
    return {"success": True, "definition": _pipeline_summary(row)}


def update_pipeline_definition(
    def_manager: PipelineDefinitionManager,
    loader: Any,
    name: str | None = None,
    definition_id: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    version: str | None = None,
    tags: list[str] | None = None,
    yaml_content: str | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    try:
        row = _resolve_pipeline(def_manager, name, definition_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    fields: dict[str, Any] = {}
    if yaml_content is not None:
        try:
            data = _parse_pipeline_yaml(yaml_content)
        except Exception as e:
            return {"success": False, "error": f"YAML validation failed: {e}"}
        fields["definition_json"] = data
        if "description" in data:
            fields["description"] = data["description"]
        if "version" in data:
            fields["version"] = str(data["version"])
        if "enabled" in data:
            fields["enabled"] = bool(data["enabled"])
    if description is not None:
        fields["description"] = description
    if enabled is not None:
        fields["enabled"] = enabled
    if version is not None:
        fields["version"] = version
    if tags is not None:
        fields["tags"] = tags
    if not fields:
        return {"success": False, "error": "No fields to update"}
    try:
        updated = def_manager.update(row.id, **fields)
    except Exception as e:
        return {"success": False, "error": f"Update failed: {e}"}
    loader.clear_cache()
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(
            cast(Any, _export_row(updated)),
            project_path,
            kind="pipeline",
            make_global=make_global_template,
        )
    except Exception as e:
        logger.warning("Failed to auto-export definition '%s': %s", updated.name, e)
    return {"success": True, "definition": _pipeline_summary(updated)}


def delete_pipeline_definition(
    def_manager: PipelineDefinitionManager,
    loader: Any,
    name: str | None = None,
    definition_id: str | None = None,
    force: bool = False,
    *,
    project_path: Path | None = None,
) -> dict[str, Any]:
    try:
        row = _resolve_pipeline(def_manager, name, definition_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    if "gobby" in (row.tags or []) and not force:
        return {
            "success": False,
            "error": (
                f"Definition '{row.name}' is bundled and will be re-created on restart. "
                "Use force=True to delete anyway."
            ),
        }
    deleted = def_manager.delete(row.id)
    if not deleted:
        return {"success": False, "error": f"Failed to delete definition '{row.name}'"}
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_delete_definition

        is_user = bool(row.tags and "user" in row.tags)
        auto_delete_definition(row.name, project_path, kind="pipeline", delete_global=is_user)
    except Exception as e:
        logger.warning("Failed to delete template '%s': %s", row.name, e)
    loader.clear_cache()
    return {"success": True, "deleted": {"id": row.id, "name": row.name}}


def export_pipeline_definition(
    def_manager: PipelineDefinitionManager,
    name: str | None = None,
    definition_id: str | None = None,
) -> dict[str, Any]:
    try:
        row = _resolve_pipeline(def_manager, name, definition_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    payload = dict(row.definition_json)
    payload.setdefault("name", row.name)
    payload.setdefault("type", "pipeline")
    payload.setdefault("version", row.version)
    payload["enabled"] = row.enabled
    if row.description is not None:
        payload.setdefault("description", row.description)
    return {
        "success": True,
        "name": row.name,
        "yaml_content": yaml.safe_dump(payload, sort_keys=False),
    }


def _auto_subscribe_lineage(
    completion_registry: Any,
    completion_id: str,
    session_id: str,
    session_manager: "SessionManager | None",
    continuation_prompt: str | None,
    db: HubDatabase | None,
) -> None:
    """Register a completion event and subscribe the calling session + its lineage.

    Also persists subscribers to DB for daemon restart recovery.
    """
    # Gather lineage session IDs (root → current)
    lineage_ids: list[str] = [session_id]
    if session_manager:
        try:
            from gobby.agents.session import ChildSessionManager

            child_mgr = ChildSessionManager(session_manager)
            lineage = child_mgr.get_session_lineage(session_id)
            lineage_ids = [s.id for s in lineage]
            # Ensure caller is included even if lineage lookup didn't find it
            if session_id not in lineage_ids:
                lineage_ids.append(session_id)
        except Exception:
            logger.debug("Could not resolve session lineage for %s", session_id, exc_info=True)

    # Register in-memory event + subscribers
    try:
        completion_registry.register(
            completion_id,
            subscribers=lineage_ids,
            continuation_prompt=continuation_prompt,
        )
    except Exception:
        logger.debug("Failed to register completion event %s", completion_id, exc_info=True)
        return

    # Persist subscribers to DB for restart recovery
    if db is not None:
        try:
            from gobby.storage.pipeline_subscribers import CompletionSubscriberManager

            # Use a lightweight manager just for subscriber CRUD.
            em = CompletionSubscriberManager(db=db)
            em.add_completion_subscribers(completion_id, lineage_ids)
        except Exception:
            logger.debug(
                "Failed to persist completion subscribers for %s", completion_id, exc_info=True
            )


def register_pipeline_tools(
    registry: InternalToolRegistry,
    loader: Any | None = None,
    executor_getter: Callable[[], Any | None] | None = None,
    execution_manager_getter: Callable[[], Any | None] | None = None,
    db: HubDatabase | None = None,
    session_manager: "SessionManager | None" = None,
    completion_registry: Any | None = None,
    def_manager: PipelineDefinitionManager | None = None,
) -> None:
    """
    Register all pipeline-related tools on an existing registry.

    Args:
        registry: The InternalToolRegistry to add pipeline tools to
        loader: PipelineLoader instance for discovering pipelines
        executor_getter: Callable returning PipelineExecutor (or None) at call time
        execution_manager_getter: Callable returning LocalPipelineExecutionManager
        db: Database instance for definition CRUD operations
        session_manager: Session manager for resolving session references
        completion_registry: CompletionEventRegistry for auto-subscribing callers
        def_manager: Typed pipeline manager for CRUD (created from db if not provided)
    """
    _loader = loader
    _get_executor = executor_getter or (lambda: None)
    _get_execution_manager = execution_manager_getter or (lambda: None)
    _def_manager = def_manager
    if _def_manager is None and db is not None:
        _def_manager = PipelineDefinitionManager(db)
    _completion_registry = completion_registry

    # Register dynamic tools for pipelines with expose_as_tool=True
    _register_exposed_pipeline_tools(
        registry,
        _loader,
        _get_executor,
        session_manager,
        completion_registry=_completion_registry,
        db=db,
    )

    @registry.tool(
        name="list_pipelines",
        description="List available pipeline definitions from project and global directories.",
    )
    async def _list_pipelines() -> dict[str, Any]:
        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None
        return await list_pipelines(_loader, project_id)

    @registry.tool(
        name="get_pipeline",
        description="Get details about a specific pipeline definition including steps and inputs.",
    )
    async def _get_pipeline(name: str) -> dict[str, Any]:
        if _loader is None:
            return {"success": False, "error": "Pipeline tools require a workflow loader"}

        from gobby.workflows.definitions import PipelineDefinition

        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None
        definition = await _loader.load_pipeline(name, project_id)

        if not definition:
            return {"success": False, "error": f"Pipeline '{name}' not found"}
        if not isinstance(definition, PipelineDefinition):
            return {"success": False, "error": f"'{name}' is a workflow, not a pipeline"}

        return {
            "success": True,
            "name": definition.name,
            "type": "pipeline",
            "description": definition.description,
            "version": definition.version,
            "enabled": definition.enabled,
            "inputs": definition.inputs,
            "outputs": definition.outputs,
            "expose_as_tool": definition.expose_as_tool,
            "steps": [
                {
                    "id": s.id,
                    "exec": s.exec,
                    "prompt": s.prompt,
                    "mcp": s.mcp.model_dump() if s.mcp else None,
                }
                for s in definition.steps
            ]
            if definition.steps
            else [],
        }

    @registry.tool(
        name="run_pipeline",
        description=(
            "Run a pipeline by name with given inputs. "
            "project_id is derived from session context. Always returns immediately "
            "with execution_id. You will be notified when the pipeline completes."
        ),
    )
    async def _run_pipeline(
        name: str,
        inputs: dict[str, Any] | None = None,
        continuation_prompt: str | None = None,
    ) -> dict[str, Any]:
        resolved_id = get_current_session_id()
        if not resolved_id:
            return {"success": False, "error": "No session context available"}

        project_ctx = get_project_context()
        project_id = project_ctx.get("id", "") if project_ctx else ""

        result = await run_pipeline(
            loader=_loader,
            executor=_get_executor(),
            name=name,
            inputs=inputs or {},
            project_id=project_id,
            session_id=resolved_id,
            continuation_prompt=continuation_prompt,
        )

        # Auto-subscribe caller session + lineage to completion events
        execution_id = result.get("execution_id")
        if result.get("success") and execution_id and _completion_registry:
            _auto_subscribe_lineage(
                _completion_registry,
                execution_id,
                resolved_id,
                session_manager,
                continuation_prompt,
                db,
            )

        return result

    @registry.tool(
        name="resume_pipeline",
        description=(
            "Resume a failed pipeline execution. Resets steps from the failure point "
            "(or from_step if specified) to PENDING, then re-executes. "
            "Only works on executions with status 'failed'."
        ),
    )
    async def _resume_pipeline(
        execution_id: str,
        from_step: str | None = None,
    ) -> dict[str, Any]:
        resolved_id = get_current_session_id()
        if not resolved_id:
            return {"success": False, "error": "No session context available"}

        project_ctx = get_project_context()
        project_id = project_ctx.get("id", "") if project_ctx else ""

        em = _get_execution_manager()
        result = await resume_pipeline(
            loader=_loader,
            executor=_get_executor(),
            execution_manager=em,
            execution_id=execution_id,
            project_id=project_id,
            session_id=resolved_id,
            from_step=from_step,
        )

        # Auto-subscribe caller session + lineage to completion events
        if result.get("success") and _completion_registry:
            _auto_subscribe_lineage(
                _completion_registry,
                execution_id,
                resolved_id,
                session_manager,
                None,
                db,
            )

        return result

    @registry.tool(
        name="approve_pipeline",
        description="Approve a pipeline execution that is waiting for approval.",
    )
    async def _approve_pipeline(
        token: str,
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        executor = _get_executor()
        if executor is None:
            return {"success": False, "error": "Pipeline executor not available"}
        return await approve_pipeline(
            executor=executor,
            token=token,
            approved_by=approved_by,
        )

    @registry.tool(
        name="reject_pipeline",
        description="Reject a pipeline execution that is waiting for approval.",
    )
    async def _reject_pipeline(
        token: str,
        rejected_by: str | None = None,
    ) -> dict[str, Any]:
        executor = _get_executor()
        if executor is None:
            return {"success": False, "error": "Pipeline executor not available"}
        return await reject_pipeline(
            executor=executor,
            token=token,
            rejected_by=rejected_by,
        )

    @registry.tool(
        name="cancel_pipeline",
        description="Cancel a running pipeline execution and kill associated agents.",
    )
    async def _cancel_pipeline(
        execution_id: str,
    ) -> dict[str, Any]:
        em = _get_execution_manager()
        if em is None:
            return {"success": False, "error": "Pipeline execution manager not available"}
        return await cancel_pipeline(
            execution_manager=em,
            execution_id=execution_id,
        )

    @registry.tool(
        name="get_pipeline_status",
        description="Get the status of a pipeline execution including step details.",
    )
    def _get_pipeline_status(
        execution_id: str,
    ) -> dict[str, Any]:
        em = _get_execution_manager()
        if em is None:
            return {"success": False, "error": "Pipeline execution manager not available"}
        return get_pipeline_status(
            execution_manager=em,
            execution_id=execution_id,
        )

    @registry.tool(
        name="list_pipeline_executions",
        description=(
            "List pipeline executions with optional filters and offset pagination. "
            "Returns a page of executions plus a filter-scoped total and status_summary. "
            "Use brief=True (default) for compact output."
        ),
    )
    def _list_pipeline_executions(
        status: str | None = None,
        pipeline_name: str | None = None,
        session_id: str | None = None,
        parent_execution_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        brief: bool = True,
        include_steps: bool = False,
    ) -> dict[str, Any]:
        em = _get_execution_manager()
        if em is None:
            return {"success": False, "error": "Pipeline execution manager not available"}
        return list_pipeline_executions(
            execution_manager=em,
            status=status,
            pipeline_name=pipeline_name,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
            limit=limit,
            offset=offset,
            brief=brief,
            include_steps=include_steps,
        )

    @registry.tool(
        name="search_pipeline_executions",
        description=(
            "Search pipeline executions by text with offset pagination. Matches "
            "pipeline names and optionally step error messages. Combine with status "
            "filter to narrow results. Returns a page plus a filter-scoped total."
        ),
    )
    def _search_pipeline_executions(
        query: str,
        search_errors: bool = True,
        search_outputs: bool = False,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_steps: bool = False,
    ) -> dict[str, Any]:
        em = _get_execution_manager()
        if em is None:
            return {"success": False, "error": "Pipeline execution manager not available"}
        return search_pipeline_executions(
            execution_manager=em,
            query=query,
            search_errors=search_errors,
            search_outputs=search_outputs,
            status=status,
            limit=limit,
            offset=offset,
            include_steps=include_steps,
        )

    @registry.tool(
        name="clear_pipeline_execution_history",
        description=(
            "Preview or clear terminal execution history for one pipeline in the "
            "current project. Defaults to a non-destructive preview; pass "
            "confirm=true to delete. Refuses deletion while any selected execution "
            "or descendant is active."
        ),
    )
    def _clear_pipeline_execution_history(
        pipeline_name: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        em = _get_execution_manager()
        if em is None:
            return {"success": False, "error": "Pipeline execution manager not available"}
        return clear_pipeline_execution_history(
            execution_manager=em,
            pipeline_name=pipeline_name,
            confirm=confirm,
        )

    @registry.tool(
        name="create_pipeline",
        description="Create a pipeline definition from YAML content. YAML must have type: pipeline.",
    )
    def _create_pipeline(
        yaml_content: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None or _loader is None:
            return {
                "success": False,
                "error": "Pipeline definition tools require database connection",
            }
        return create_pipeline_definition(_def_manager, _loader, yaml_content, project_id)

    @registry.tool(
        name="update_pipeline",
        description="Update a pipeline definition by name or ID. Accepts field updates and/or full YAML replacement.",
    )
    def _update_pipeline(
        name: str | None = None,
        definition_id: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        version: str | None = None,
        tags: list[str] | None = None,
        yaml_content: str | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None or _loader is None:
            return {
                "success": False,
                "error": "Pipeline definition tools require database connection",
            }
        err = _require_pipeline(_def_manager, name, definition_id)
        if err:
            return err
        return update_pipeline_definition(
            _def_manager,
            _loader,
            name,
            definition_id,
            description,
            enabled,
            version,
            tags,
            yaml_content,
        )

    @registry.tool(
        name="delete_pipeline",
        description="Delete a pipeline definition by name or ID. Bundled definitions are protected unless force=True.",
    )
    def _delete_pipeline(
        name: str | None = None,
        definition_id: str | None = None,
        force: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None or _loader is None:
            return {
                "success": False,
                "error": "Pipeline definition tools require database connection",
            }
        err = _require_pipeline(_def_manager, name, definition_id)
        if err:
            return err
        return delete_pipeline_definition(
            _def_manager,
            _loader,
            name,
            definition_id,
            force,
            project_path=Path(project_path) if project_path else None,
        )

    @registry.tool(
        name="export_pipeline",
        description="Export a pipeline definition as YAML content.",
    )
    def _export_pipeline(
        name: str | None = None,
        definition_id: str | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {
                "success": False,
                "error": "Pipeline definition tools require database connection",
            }
        err = _require_pipeline(_def_manager, name, definition_id)
        if err:
            return err
        return export_pipeline_definition(_def_manager, name, definition_id)


def _register_exposed_pipeline_tools(
    registry: InternalToolRegistry,
    loader: Any | None,
    executor_getter: Callable[[], Any | None],
    session_manager: "SessionManager | None" = None,
    completion_registry: Any | None = None,
    db: HubDatabase | None = None,
) -> None:
    """
    Register dynamic tools for pipelines with expose_as_tool=True.

    Each exposed pipeline becomes an MCP tool named "pipeline:<pipeline_name>".
    """
    if loader is None:
        logger.debug("Skipping dynamic pipeline tools: no loader")
        return

    try:
        discovered = loader.discover_pipelines_sync()
    except Exception:
        logger.warning("Failed to discover pipelines for dynamic tools", exc_info=True)
        return

    for workflow in discovered:
        pipeline = workflow.definition

        # Disabled pipelines must not remain callable through dynamic tools.
        if not getattr(pipeline, "enabled", False):
            continue

        # Only expose pipelines with expose_as_tool=True
        if not getattr(pipeline, "expose_as_tool", False):
            continue

        _create_pipeline_tool(
            registry,
            pipeline,
            loader,
            executor_getter,
            session_manager,
            completion_registry=completion_registry,
            db=db,
        )


def _create_pipeline_tool(
    registry: InternalToolRegistry,
    pipeline: Any,
    loader: Any,
    executor_getter: Callable[[], Any | None],
    session_manager: "SessionManager | None" = None,
    completion_registry: Any | None = None,
    db: HubDatabase | None = None,
) -> None:
    """Create a dynamic tool for a single pipeline."""
    _completion_registry = completion_registry
    tool_name = f"pipeline:{pipeline.name}"
    description = pipeline.description or f"Run the {pipeline.name} pipeline"

    # Build input schema from pipeline inputs
    input_schema = _build_input_schema(pipeline)

    # Create closure to capture pipeline name
    pipeline_name = pipeline.name

    async def _execute_pipeline(**kwargs: Any) -> dict[str, Any]:
        # Pop meta-parameters (session_id may still arrive from old callers)
        kwargs.pop("session_id", None)
        continuation_prompt = kwargs.pop("continuation_prompt", None)

        resolved_id = get_current_session_id()
        if not resolved_id:
            return {"success": False, "error": "No session context available"}

        project_ctx = get_project_context()
        project_id = project_ctx.get("id", "") if project_ctx else ""

        result = await run_pipeline(
            loader=loader,
            executor=executor_getter(),
            name=pipeline_name,
            inputs=kwargs,
            project_id=project_id,
            session_id=resolved_id,
            continuation_prompt=continuation_prompt,
        )

        # Auto-subscribe caller session + lineage to completion events
        execution_id = result.get("execution_id")
        if result.get("success") and execution_id and _completion_registry:
            _auto_subscribe_lineage(
                _completion_registry,
                execution_id,
                resolved_id,
                session_manager,
                continuation_prompt,
                db,
            )

        return result

    # Register the tool with the schema
    registry.register(
        name=tool_name,
        description=description,
        func=_execute_pipeline,
        input_schema=input_schema,
    )

    logger.debug("Registered dynamic pipeline tool: %s", tool_name)


def _build_input_schema(pipeline: Any) -> dict[str, Any]:
    """Build JSON Schema for pipeline inputs."""
    properties = {}
    required = []

    for name, input_def in pipeline.inputs.items():
        if isinstance(input_def, dict):
            # Input is already a schema-like dict
            prop = {}
            if "type" in input_def:
                prop["type"] = input_def["type"]
            else:
                prop["type"] = "string"

            if "description" in input_def:
                prop["description"] = input_def["description"]

            if "default" in input_def:
                prop["default"] = input_def["default"]
            else:
                # No default means required
                required.append(name)

            properties[name] = prop
        else:
            # Input is a simple default value
            properties[name] = {
                "type": "string",
                "default": input_def,
            }

    # Add continuation_prompt as optional meta-parameter
    properties["continuation_prompt"] = {
        "type": "string",
        "description": (
            "Instructions for what to do when the pipeline completes. "
            "Included in the completion notification sent to subscribers."
        ),
    }

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }

    if required:
        schema["required"] = required

    return schema
