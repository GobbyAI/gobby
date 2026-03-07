"""
Pipeline tool registration for the unified gobby-workflows server.

Contains helpers and a register_pipeline_tools() function that adds all
pipeline-related MCP tools to a given InternalToolRegistry.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.workflows._definitions import (
    _resolve_definition,
    create_workflow_definition,
    delete_workflow_definition,
    export_workflow_definition,
    update_workflow_definition,
)
from gobby.mcp_proxy.tools.workflows._pipeline_discovery import list_pipelines
from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    approve_pipeline,
    get_pipeline_status,
    reject_pipeline,
    resume_pipeline,
    run_pipeline,
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
    try:
        row = _resolve_definition(def_manager, name, definition_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    if row.workflow_type != "pipeline":
        return {"success": False, "error": f"'{row.name}' is a workflow, not a pipeline"}
    return None


def _auto_subscribe_lineage(
    completion_registry: Any,
    completion_id: str,
    session_id: str,
    session_manager: "LocalSessionManager | None",
    continuation_prompt: str | None,
    db: DatabaseProtocol | None,
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
            from gobby.storage.pipelines import LocalPipelineExecutionManager

            # Use a lightweight manager just for subscriber CRUD
            em = LocalPipelineExecutionManager(db=db, project_id="")
            em.add_completion_subscribers(completion_id, lineage_ids)
        except Exception:
            logger.debug(
                "Failed to persist completion subscribers for %s", completion_id, exc_info=True
            )


def _resolve_session_ref(ref: str, session_manager: "LocalSessionManager | None") -> str:
    """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
    if session_manager is None:
        logger.warning("session_manager is None; returning raw session ref %r", ref)
        return ref
    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context()
    project_id = project_ctx.get("id") if project_ctx else None
    return str(session_manager.resolve_session_reference(ref, project_id))


def register_pipeline_tools(
    registry: InternalToolRegistry,
    loader: Any | None = None,
    executor_getter: Callable[[], Any | None] | None = None,
    execution_manager_getter: Callable[[], Any | None] | None = None,
    db: DatabaseProtocol | None = None,
    session_manager: "LocalSessionManager | None" = None,
    completion_registry: Any | None = None,
    def_manager: LocalWorkflowDefinitionManager | None = None,
) -> None:
    """
    Register all pipeline-related tools on an existing registry.

    Args:
        registry: The InternalToolRegistry to add pipeline tools to
        loader: WorkflowLoader instance for discovering pipelines
        executor_getter: Callable returning PipelineExecutor (or None) at call time
        execution_manager_getter: Callable returning LocalPipelineExecutionManager
        db: Database instance for definition CRUD operations
        session_manager: Session manager for resolving session references
        completion_registry: CompletionEventRegistry for auto-subscribing callers
        def_manager: Definition manager for pipeline CRUD (created from db if not provided)
    """
    _loader = loader
    _get_executor = executor_getter or (lambda: None)
    _get_execution_manager = execution_manager_getter or (lambda: None)
    _def_manager = def_manager
    if _def_manager is None and db is not None:
        _def_manager = LocalWorkflowDefinitionManager(db)
    _completion_registry = completion_registry

    def _resolve_session(ref: str) -> str:
        """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
        return _resolve_session_ref(ref, session_manager)

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
        name="wait_for_completion",
        description=(
            "Block until a completion event fires (agent run or pipeline execution). "
            "Returns the result when the event completes, or an error on timeout. "
            "The completion_id is typically a run_id (from spawn_agent) or "
            "execution_id (from run_pipeline)."
        ),
    )
    async def _wait_for_completion(
        completion_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if _completion_registry is None:
            return {"success": False, "error": "Completion registry not available"}
        try:
            result = await _completion_registry.wait(completion_id, timeout=timeout)
            return {"success": True, "completion_id": completion_id, **result}
        except KeyError:
            return {
                "success": False,
                "error": f"Completion event {completion_id!r} not registered. "
                "Ensure the agent or pipeline was started first.",
            }
        except TimeoutError:
            return {
                "success": False,
                "error": f"Timed out waiting for completion event {completion_id!r}",
                "timeout": timeout,
            }

    @registry.tool(
        name="register_pipeline_continuation",
        description=(
            "Register pipeline continuations for dispatched agents. When any agent "
            "completes, the specified pipeline is re-invoked with the given inputs. "
            "Used by the orchestrator for event-driven re-invocation instead of polling."
        ),
    )
    async def _register_pipeline_continuation(
        dispatch_outputs: dict[str, Any],
        pipeline_name: str,
        inputs: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if _completion_registry is None:
            return {"success": False, "error": "Completion registry not available"}

        # Extract run_ids from dispatch outputs
        run_ids: list[str] = []

        developers_output = dispatch_outputs.get("developers")
        if developers_output:
            # dispatch_batch returns {results: [{run_id, ...}, ...]}
            results = developers_output.get("results", [])
            if isinstance(results, list):
                for r in results:
                    if isinstance(r, dict) and r.get("run_id"):
                        run_ids.append(r["run_id"])

        qa_output = dispatch_outputs.get("qa")
        if qa_output and isinstance(qa_output, dict) and qa_output.get("run_id"):
            run_ids.append(qa_output["run_id"])

        merge_output = dispatch_outputs.get("merge")
        if merge_output and isinstance(merge_output, dict) and merge_output.get("run_id"):
            run_ids.append(merge_output["run_id"])

        if not run_ids:
            return {
                "success": True,
                "registered": 0,
                "message": "No active agents to register continuations for",
            }

        # Resolve session and project context
        resolved_session_id = None
        project_id = ""
        if session_id:
            try:
                resolved_session_id = _resolve_session_ref(session_id, session_manager)
            except ValueError:
                resolved_session_id = session_id

            if session_manager is not None and resolved_session_id:
                try:
                    session = session_manager.get(resolved_session_id)
                    if session:
                        project_id = session.project_id
                except Exception:
                    logger.warning(
                        "Failed to look up session %s for project_id",
                        resolved_session_id,
                        exc_info=True,
                    )

        continuation_config = {
            "pipeline_name": pipeline_name,
            "inputs": inputs,
            "session_id": resolved_session_id or session_id,
            "project_id": project_id,
        }

        registered = 0
        for run_id in run_ids:
            # Ensure the completion event is registered before adding continuation
            if not _completion_registry.is_registered(run_id):
                _completion_registry.register(run_id, subscribers=[])
            _completion_registry.register_continuation(run_id, continuation_config)
            registered += 1

        return {
            "success": True,
            "registered": registered,
            "run_ids": run_ids,
            "pipeline_name": pipeline_name,
        }

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
            "project_id is derived from the session. Always returns immediately "
            "with execution_id. You will be notified when the pipeline completes."
        ),
    )
    async def _run_pipeline(
        name: str,
        session_id: str,
        inputs: dict[str, Any] | None = None,
        continuation_prompt: str | None = None,
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
        session_id: str,
        from_step: str | None = None,
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


def _register_exposed_pipeline_tools(
    registry: InternalToolRegistry,
    loader: Any | None,
    executor_getter: Callable[[], Any | None],
    session_manager: "LocalSessionManager | None" = None,
    completion_registry: Any | None = None,
    db: DatabaseProtocol | None = None,
) -> None:
    """
    Register dynamic tools for pipelines with expose_as_tool=True.

    Each exposed pipeline becomes an MCP tool named "pipeline:<pipeline_name>".
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
    session_manager: "LocalSessionManager | None" = None,
    completion_registry: Any | None = None,
    db: DatabaseProtocol | None = None,
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
        session_id = kwargs.pop("session_id", None)
        continuation_prompt = kwargs.pop("continuation_prompt", None)
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

    logger.debug(f"Registered dynamic pipeline tool: {tool_name}")


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

    # Add session_id as a required meta-parameter for all exposed pipelines
    properties["session_id"] = {
        "type": "string",
        "description": "Session ID of the caller (required; project_id is derived from this)",
    }
    required.append("session_id")

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
