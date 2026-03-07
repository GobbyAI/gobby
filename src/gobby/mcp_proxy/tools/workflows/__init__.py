"""
Internal MCP tools for Gobby Workflow System.

Umbrella server for workflows, pipelines, rules, variables, and agent definitions.

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

from collections.abc import Callable
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.workflows._agents import (
    create_agent_definition,
    delete_agent_definition,
    get_agent_definition,
    list_agent_definitions,
    toggle_agent_definition,
    update_agent_rules,
    update_agent_steps,
    update_agent_variables,
)
from gobby.mcp_proxy.tools.workflows._definitions import (
    create_workflow_definition,
    delete_workflow_definition,
    export_workflow_definition,
    restore_workflow_definition,
    update_workflow_definition,
)
from gobby.mcp_proxy.tools.workflows._import import import_workflow, reload_cache
from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools
from gobby.mcp_proxy.tools.workflows._query import (
    get_workflow,
    get_workflow_status,
    list_workflows,
)
from gobby.mcp_proxy.tools.workflows._rules import (
    create_rule,
    delete_rule,
    get_rule,
    list_rules,
    toggle_rule,
)
from gobby.mcp_proxy.tools.workflows._variables import (
    set_session_variable,
)
from gobby.storage.database import DatabaseProtocol
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.utils.project_context import get_workflow_project_path
from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.state_manager import (
    SessionVariableManager,
    WorkflowInstanceManager,
)

__all__ = [
    "create_workflows_registry",
    "get_workflow_project_path",
]


def create_workflows_registry(
    loader: WorkflowLoader | None = None,
    session_manager: LocalSessionManager | None = None,
    db: DatabaseProtocol | None = None,
    # Pipeline dependencies (resolved lazily at call time)
    executor_getter: Callable[[], Any | None] | None = None,
    execution_manager_getter: Callable[[], Any | None] | None = None,
    completion_registry: Any | None = None,
) -> InternalToolRegistry:
    """
    Create a workflow tool registry with all workflow-related tools.

    This is the umbrella registry for workflows, pipelines, rules,
    variables, and agent definitions.

    Args:
        loader: WorkflowLoader instance
        session_manager: LocalSessionManager instance (created from db if not provided)
        db: Database instance for creating default managers
        executor_getter: Callable returning PipelineExecutor (or None) at call time
        execution_manager_getter: Callable returning LocalPipelineExecutionManager
        completion_registry: CompletionEventRegistry for pipeline auto-subscriptions

    Returns:
        InternalToolRegistry with workflow, pipeline, rule, and agent definition tools
    """
    _db = db
    _loader = loader or WorkflowLoader(db=_db)

    if session_manager is not None:
        _session_manager = session_manager
    elif _db is not None:
        _session_manager = LocalSessionManager(_db)
    else:
        _session_manager = None

    # Create multi-workflow managers
    _instance_manager = WorkflowInstanceManager(_db) if _db is not None else None
    _session_var_manager = SessionVariableManager(_db) if _db is not None else None
    _def_manager = LocalWorkflowDefinitionManager(_db) if _db is not None else None

    registry = InternalToolRegistry(
        name="gobby-workflows",
        description="Workflow management - list, activate, status, transition, end",
    )

    @registry.tool(
        name="get_workflow",
        description="Get details about a specific workflow definition.",
    )
    async def _get_workflow(
        name: str,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await get_workflow(_loader, name, project_path)

    @registry.tool(
        name="list_workflows",
        description="List available workflow definitions from project and global directories.",
    )
    def _list_workflows(
        project_path: str | None = None,
        workflow_type: str | None = None,
        global_only: bool = False,
    ) -> dict[str, Any]:
        return list_workflows(_loader, project_path, workflow_type, global_only, db=_db)

    @registry.tool(
        name="get_workflow_status",
        description="Get workflow status for a session. Shows all active workflow instances and session variables. Accepts #N, N, UUID, or prefix for session_id.",
    )
    def _get_workflow_status(session_id: str | None = None) -> dict[str, Any]:
        if _session_manager is None:
            return {"error": "Workflow tools require database connection"}
        return get_workflow_status(
            _session_manager,
            session_id,
            instance_manager=_instance_manager,
            session_var_manager=_session_var_manager,
        )

    @registry.tool(
        name="set_session_variable",
        description="Set a session-scoped shared variable (visible to all workflows). Accepts #N, N, UUID, or prefix for session_id.",
    )
    def _set_session_variable(
        name: str,
        value: str | int | float | bool | None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if _session_manager is None or _session_var_manager is None:
            return {"error": "Workflow tools require database connection"}
        return set_session_variable(
            _session_manager,
            _session_var_manager,
            name,
            value,
            session_id,
        )

    @registry.tool(
        name="evaluate_workflow",
        description="Validate a workflow definition — structural and semantic checks without executing.",
    )
    async def _evaluate_workflow(
        name: str,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Validate a workflow definition for structural and semantic issues.

        Checks for unreachable steps, dead-end steps, undefined transition targets,
        undefined variable references, MCP tool conflicts, and unknown MCP servers/tools.

        Args:
            name: Workflow name to evaluate.
            project_path: Optional project path for resolution.

        Returns:
            Dict with valid bool, items list, step_trace, and lifecycle_path.
        """
        from gobby.workflows.dry_run import evaluate_workflow

        # Try to get MCP manager for semantic checks
        mcp_mgr = None

        resolved_path: str | None = project_path
        if not resolved_path:
            path = get_workflow_project_path()
            resolved_path = str(path) if path else None

        eval_result = await evaluate_workflow(
            name,
            _loader,
            resolved_path,
            mcp_mgr,
        )
        return eval_result.to_dict()

    @registry.tool(
        name="import_workflow",
        description="Import a workflow from a file path into the project or global directory.",
    )
    def _import_workflow(
        source_path: str,
        workflow_name: str | None = None,
        is_global: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return import_workflow(_loader, source_path, workflow_name, is_global, project_path)

    @registry.tool(
        name="reload_cache",
        description="Clear the workflow cache and re-sync bundled workflows to DB. Use this after modifying workflow YAML files.",
    )
    def _reload_cache() -> dict[str, Any]:
        return reload_cache(_loader, db=_db)

    @registry.tool(
        name="create_workflow",
        description="Create a workflow or pipeline definition from YAML content. Validates with Pydantic before inserting into the database.",
    )
    def _create_workflow(
        yaml_content: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Definition tools require database connection"}
        return create_workflow_definition(_def_manager, _loader, yaml_content, project_id)

    @registry.tool(
        name="update_workflow",
        description="Update a workflow or pipeline definition by name or ID. Accepts individual field updates and/or full YAML replacement.",
    )
    def _update_workflow(
        name: str | None = None,
        definition_id: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
        version: str | None = None,
        tags: list[str] | None = None,
        yaml_content: str | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Definition tools require database connection"}
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
        name="delete_workflow",
        description="Delete a workflow or pipeline definition by name or ID. Bundled definitions are protected unless force=True.",
    )
    def _delete_workflow(
        name: str | None = None,
        definition_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Definition tools require database connection"}
        return delete_workflow_definition(_def_manager, _loader, name, definition_id, force)

    @registry.tool(
        name="export_workflow",
        description="Export a workflow or pipeline definition as YAML content.",
    )
    def _export_workflow(
        name: str | None = None,
        definition_id: str | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Definition tools require database connection"}
        return export_workflow_definition(_def_manager, name, definition_id)

    @registry.tool(
        name="restore_workflow",
        description="Restore a soft-deleted workflow or pipeline definition by name or ID.",
    )
    def _restore_workflow(
        name: str | None = None,
        definition_id: str | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Definition tools require database connection"}
        return restore_workflow_definition(_def_manager, _loader, name, definition_id)

    # ── Rule tools ──

    @registry.tool(
        name="list_rules",
        description="List standalone rules. Supports filtering by event, group, and enabled status. Use brief=True for minimal output (name, event, group, enabled only).",
    )
    def _list_rules(
        event: str | None = None,
        group: str | None = None,
        enabled: bool | None = None,
        brief: bool = False,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Rule tools require database connection"}
        return list_rules(_def_manager, event, group, enabled, brief=brief)

    @registry.tool(
        name="get_rule",
        description="Get full details of a standalone rule by name.",
    )
    def _get_rule(name: str) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Rule tools require database connection"}
        return get_rule(_def_manager, name)

    @registry.tool(
        name="toggle_rule",
        description="Enable or disable a standalone rule by name.",
    )
    def _toggle_rule(name: str, enabled: bool) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Rule tools require database connection"}
        return toggle_rule(_def_manager, name, enabled)

    @registry.tool(
        name="create_rule",
        description="Create a new standalone rule. Validates definition with RuleDefinitionBody before inserting.",
    )
    def _create_rule(
        name: str,
        definition: dict[str, Any],
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Rule tools require database connection"}
        return create_rule(_def_manager, name, definition)

    @registry.tool(
        name="delete_rule",
        description="Delete a standalone rule by name (soft-delete). Bundled rules are protected unless force=True.",
    )
    def _delete_rule(
        name: str,
        force: bool = False,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Rule tools require database connection"}
        return delete_rule(_def_manager, name, force)

    # ── Agent definition CRUD tools ──

    @registry.tool(
        name="list_agent_definitions",
        description="List agent definitions. Supports filtering by enabled status and project ID.",
    )
    def _list_agent_definitions(
        enabled: bool | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return list_agent_definitions(_def_manager, enabled, project_id)

    @registry.tool(
        name="get_agent_definition",
        description="Get full details of an agent definition by name.",
    )
    def _get_agent_definition(name: str) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return get_agent_definition(_def_manager, name)

    @registry.tool(
        name="create_agent_definition",
        description="Create a new agent definition. Validates with AgentDefinitionBody before inserting.",
    )
    def _create_agent_definition(
        name: str,
        definition: dict[str, Any],
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return create_agent_definition(_def_manager, name, definition)

    @registry.tool(
        name="toggle_agent_definition",
        description="Enable or disable an agent definition by name.",
    )
    def _toggle_agent_definition(name: str, enabled: bool) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return toggle_agent_definition(_def_manager, name, enabled)

    @registry.tool(
        name="delete_agent_definition",
        description="Delete an agent definition by name (soft-delete). Template agents are protected unless force=True.",
    )
    def _delete_agent_definition(
        name: str,
        force: bool = False,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return delete_agent_definition(_def_manager, name, force)

    @registry.tool(
        name="update_agent_rules",
        description="Add or remove rules from an agent definition's workflows.rules list.",
    )
    def _update_agent_rules(
        name: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return update_agent_rules(_def_manager, name, add, remove)

    @registry.tool(
        name="update_agent_variables",
        description="Set or remove variables from an agent definition's workflows.variables dict.",
    )
    def _update_agent_variables(
        name: str,
        set_vars: dict[str, Any] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return update_agent_variables(_def_manager, name, set_vars, remove)

    @registry.tool(
        name="update_agent_steps",
        description="Replace an agent's inline step workflow steps. Pass steps list or None to clear.",
    )
    def _update_agent_steps(
        name: str,
        steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if _def_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return update_agent_steps(_def_manager, name, steps)

    # ── Pipeline utility tools ──

    @registry.tool(
        name="fail_pipeline",
        description="Fail the current pipeline step with an error message. Used as a guard step to halt execution when a condition is met.",
    )
    def _fail_pipeline(message: str) -> dict[str, Any]:
        return {"success": False, "error": message}

    @registry.tool(
        name="pipeline_eval",
        description="Evaluate and return structured data within a pipeline. Pass a dict of key-value pairs; they become the step output. Use with template expressions to compute values from prior step outputs.",
    )
    def _pipeline_eval(data: dict[str, Any]) -> dict[str, Any]:
        # Coerce string booleans/numbers from template rendering
        coerced: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, str):
                if v.lower() == "true":
                    coerced[k] = True
                elif v.lower() == "false":
                    coerced[k] = False
                else:
                    try:
                        coerced[k] = int(v)
                    except ValueError:
                        try:
                            coerced[k] = float(v)
                        except ValueError:
                            coerced[k] = v
            else:
                coerced[k] = v
        return coerced

    # ── Pipeline tools ──

    register_pipeline_tools(
        registry,
        loader=_loader,
        executor_getter=executor_getter,
        execution_manager_getter=execution_manager_getter,
        db=_db,
        session_manager=_session_manager,
        completion_registry=completion_registry,
        def_manager=_def_manager,
    )

    return registry
