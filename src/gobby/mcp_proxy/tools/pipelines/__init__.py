"""
Internal MCP tools for Gobby Pipeline System.

Exposes functionality for:
- list_pipelines: Discover available pipeline definitions
- Dynamic pipeline tools: Pipelines with expose_as_tool=True are exposed as MCP tools

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.pipelines._discovery import list_pipelines
from gobby.mcp_proxy.tools.pipelines._execution import (
    approve_pipeline,
    get_pipeline_status,
    reject_pipeline,
    run_pipeline,
)
from gobby.mcp_proxy.tools.workflows._definitions import (
    create_workflow_definition,
    delete_workflow_definition,
    export_workflow_definition,
    update_workflow_definition,
)
from gobby.storage.database import DatabaseProtocol
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

if TYPE_CHECKING:
    from gobby.storage.sessions import LocalSessionManager

logger = logging.getLogger(__name__)


def _require_pipeline(
    def_manager: LocalWorkflowDefinitionManager,
    name: str | None = None,
    definition_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a definition and verify it's a pipeline. Returns error dict or None."""
    from gobby.mcp_proxy.tools.workflows._definitions import _resolve_definition

    try:
        row = _resolve_definition(def_manager, name, definition_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    if row.workflow_type != "pipeline":
        return {"success": False, "error": f"'{row.name}' is a workflow, not a pipeline"}
    return None


__all__ = [
    "create_pipelines_registry",
]


def _resolve_session_ref(ref: str, session_manager: "LocalSessionManager | None") -> str:
    """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
    if session_manager is None:
        logger.warning("session_manager is None; returning raw session ref %r", ref)
        return ref
    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context()
    project_id = project_ctx.get("id") if project_ctx else None
    return str(session_manager.resolve_session_reference(ref, project_id))


def create_pipelines_registry(
    loader: Any | None = None,
    executor_getter: Callable[[], Any | None] | None = None,
    execution_manager_getter: Callable[[], Any | None] | None = None,
    db: DatabaseProtocol | None = None,
    session_manager: "LocalSessionManager | None" = None,
) -> InternalToolRegistry:
    """
    Create a pipeline tool registry with all pipeline-related tools.

    Args:
        loader: WorkflowLoader instance for discovering pipelines
        executor_getter: Callable returning PipelineExecutor (or None) at call time.
            Resolved lazily so the registry can be created before the executor exists.
        execution_manager_getter: Callable returning LocalPipelineExecutionManager
            (or None) at call time. Resolved lazily like executor_getter.
        db: Database instance for definition CRUD operations
        session_manager: Session manager for resolving session references

    Returns:
        InternalToolRegistry with pipeline tools registered
    """
    _loader = loader
    _get_executor = executor_getter or (lambda: None)
    _get_execution_manager = execution_manager_getter or (lambda: None)
    _def_manager = LocalWorkflowDefinitionManager(db) if db is not None else None

    def _resolve_session(ref: str) -> str:
        """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
        return _resolve_session_ref(ref, session_manager)

    registry = InternalToolRegistry(
        name="gobby-pipelines",
        description="Pipeline management - list, run, and monitor pipeline executions",
    )

    # Register dynamic tools for pipelines with expose_as_tool=True
    _register_exposed_pipeline_tools(registry, _loader, _get_executor, session_manager)

    @registry.tool(
        name="list_pipelines",
        description="List available pipeline definitions from project and global directories.",
    )
    async def _list_pipelines(
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await list_pipelines(_loader, project_path)

    @registry.tool(
        name="get_pipeline",
        description="Get details about a specific pipeline definition including steps and inputs.",
    )
    async def _get_pipeline(
        name: str,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        if _loader is None:
            return {"success": False, "error": "Pipeline tools require a workflow loader"}

        from pathlib import Path

        from gobby.utils.project_context import get_workflow_project_path
        from gobby.workflows.definitions import PipelineDefinition

        if not project_path:
            discovered = get_workflow_project_path()
            if discovered:
                project_path = str(discovered)

        proj = Path(project_path) if project_path else None
        definition = await _loader.load_workflow(name, proj)

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
            "Run a pipeline by name with given inputs. Requires session_id; "
            "project_id is derived from the session. Set wait=true to block "
            "until the pipeline completes and return full results with all "
            "step outputs."
        ),
    )
    async def _run_pipeline(
        name: str,
        session_id: str,
        inputs: dict[str, Any] | None = None,
        wait: bool = False,
        wait_timeout: int = 300,
    ) -> dict[str, Any]:
        # Resolve session reference and derive project_id
        try:
            resolved_id = _resolve_session(session_id)
        except ValueError as e:
            return {"success": False, "error": f"Invalid session_id: {e}"}

        project_id = ""
        if session_manager is not None:
            session = await asyncio.to_thread(session_manager.get, resolved_id)
            if session is None:
                return {"success": False, "error": f"Session '{session_id}' not found"}
            project_id = session.project_id

        return await run_pipeline(
            loader=_loader,
            executor=_get_executor(),
            name=name,
            inputs=inputs or {},
            project_id=project_id,
            session_id=resolved_id,
            wait=wait,
            wait_timeout=wait_timeout,
        )

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
            return {"error": "Pipeline executor not available"}
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
            return {"error": "Pipeline executor not available"}
        return await reject_pipeline(
            executor=executor,
            token=token,
            rejected_by=rejected_by,
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
            return {"error": "Pipeline execution manager not available"}
        return get_pipeline_status(
            execution_manager=em,
            execution_id=execution_id,
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
        import yaml as _yaml

        try:
            data = _yaml.safe_load(yaml_content)
        except _yaml.YAMLError as e:
            return {"success": False, "error": f"Invalid YAML: {e}"}
        if not isinstance(data, dict) or data.get("type") != "pipeline":
            return {"success": False, "error": "YAML must have 'type: pipeline'"}
        return create_workflow_definition(_def_manager, _loader, yaml_content, project_id)

    @registry.tool(
        name="update_pipeline",
        description="Update a pipeline definition by name or ID. Accepts field updates and/or full YAML replacement.",
    )
    def _update_pipeline(
        name: str | None = None,
        definition_id: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
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
        return update_workflow_definition(
            _def_manager,
            _loader,
            name,
            definition_id,
            description,
            enabled,
            priority,
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
    ) -> dict[str, Any]:
        if _def_manager is None or _loader is None:
            return {
                "success": False,
                "error": "Pipeline definition tools require database connection",
            }
        err = _require_pipeline(_def_manager, name, definition_id)
        if err:
            return err
        return delete_workflow_definition(_def_manager, _loader, name, definition_id, force)

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
        return export_workflow_definition(_def_manager, name, definition_id)

    return registry


def _register_exposed_pipeline_tools(
    registry: InternalToolRegistry,
    loader: Any | None,
    executor_getter: Callable[[], Any | None],
    session_manager: "LocalSessionManager | None" = None,
) -> None:
    """
    Register dynamic tools for pipelines with expose_as_tool=True.

    Each exposed pipeline becomes an MCP tool named "pipeline:<pipeline_name>".

    Args:
        registry: The registry to add tools to
        loader: WorkflowLoader for discovering pipelines
        executor_getter: Callable returning PipelineExecutor at call time
        session_manager: Session manager for resolving session references
    """
    if loader is None:
        logger.debug("Skipping dynamic pipeline tools: no loader")
        return

    try:
        discovered = loader.discover_pipeline_workflows_sync()
    except Exception:
        logger.warning("Failed to discover pipelines for dynamic tools", exc_info=True)
        return

    for workflow in discovered:
        pipeline = workflow.definition

        # Only expose pipelines with expose_as_tool=True
        if not getattr(pipeline, "expose_as_tool", False):
            continue

        _create_pipeline_tool(registry, pipeline, loader, executor_getter, session_manager)


def _create_pipeline_tool(
    registry: InternalToolRegistry,
    pipeline: Any,
    loader: Any,
    executor_getter: Callable[[], Any | None],
    session_manager: "LocalSessionManager | None" = None,
) -> None:
    """
    Create a dynamic tool for a single pipeline.

    Args:
        registry: The registry to add the tool to
        pipeline: The PipelineDefinition to expose
        loader: WorkflowLoader for loading pipelines
        executor_getter: Callable returning PipelineExecutor at call time
        session_manager: Session manager for resolving session references
    """
    tool_name = f"pipeline:{pipeline.name}"
    description = pipeline.description or f"Run the {pipeline.name} pipeline"

    # Build input schema from pipeline inputs
    input_schema = _build_input_schema(pipeline)

    # Create closure to capture pipeline name
    pipeline_name = pipeline.name

    async def _execute_pipeline(**kwargs: Any) -> dict[str, Any]:
        session_id = kwargs.pop("session_id", None)
        wait = kwargs.pop("wait", False)
        wait_timeout = kwargs.pop("wait_timeout", 300)
        if not session_id:
            return {"success": False, "error": "session_id is required"}

        # Resolve session reference and derive project_id
        try:
            resolved_id = _resolve_session_ref(session_id, session_manager)
        except ValueError as e:
            return {"success": False, "error": f"Invalid session_id: {e}"}

        project_id = ""
        if session_manager is not None:
            session = await asyncio.to_thread(session_manager.get, resolved_id)
            if session is None:
                return {"success": False, "error": f"Session '{session_id}' not found"}
            project_id = session.project_id

        return await run_pipeline(
            loader=loader,
            executor=executor_getter(),
            name=pipeline_name,
            inputs=kwargs,
            project_id=project_id,
            session_id=resolved_id,
            wait=wait,
            wait_timeout=wait_timeout,
        )

    # Register the tool with the schema
    registry.register(
        name=tool_name,
        description=description,
        func=_execute_pipeline,
        input_schema=input_schema,
    )

    logger.debug(f"Registered dynamic pipeline tool: {tool_name}")


def _build_input_schema(pipeline: Any) -> dict[str, Any]:
    """
    Build JSON Schema for pipeline inputs.

    Args:
        pipeline: The PipelineDefinition

    Returns:
        JSON Schema dict for the pipeline's inputs
    """
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

    # Add session_id as a required meta-parameter for all exposed pipelines
    properties["session_id"] = {
        "type": "string",
        "description": "Session ID of the caller (required; project_id is derived from this)",
    }
    required.append("session_id")

    # Add wait/wait_timeout as optional meta-parameters
    properties["wait"] = {
        "type": "boolean",
        "description": "If true, block until pipeline completes and return full results (default: false)",
        "default": False,
    }
    properties["wait_timeout"] = {
        "type": "integer",
        "description": "Max seconds to wait when wait=true (default: 300)",
        "default": 300,
    }

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }

    if required:
        schema["required"] = required

    return schema
