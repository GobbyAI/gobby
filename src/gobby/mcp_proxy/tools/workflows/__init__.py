"""
Internal MCP tools for Gobby Workflow System.

Exposes functionality for:
- get_workflow: Get details about a specific workflow definition
- list_workflows: Discover available workflow definitions
- activate_workflow: Start a step-based workflow (supports initial variables)
- end_workflow: Complete/terminate active workflow
- get_workflow_status: Get current workflow state
- request_step_transition: Request transition to a different step
- set_variable: Set a workflow variable for the session
- get_variable: Get workflow variable(s) for the session
- import_workflow: Import a workflow from a file path
- reload_cache: Clear the workflow loader cache to pick up file changes

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.workflows._definitions import (
    create_workflow_definition,
    delete_workflow_definition,
    export_workflow_definition,
    restore_workflow_definition,
    update_workflow_definition,
)
from gobby.mcp_proxy.tools.workflows._import import import_workflow, reload_cache
from gobby.mcp_proxy.tools.workflows._lifecycle import (
    activate_workflow,
    end_workflow,
    request_step_transition,
)
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
    get_variable,
    set_session_variable,
    set_variable,
)
from gobby.storage.database import DatabaseProtocol
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.utils.project_context import get_workflow_project_path
from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.state_manager import (
    SessionVariableManager,
    WorkflowInstanceManager,
    WorkflowStateManager,
)

__all__ = [
    "create_workflows_registry",
    "get_workflow_project_path",
]


def create_workflows_registry(
    loader: WorkflowLoader | None = None,
    state_manager: WorkflowStateManager | None = None,
    session_manager: LocalSessionManager | None = None,
    db: DatabaseProtocol | None = None,
) -> InternalToolRegistry:
    """
    Create a workflow tool registry with all workflow-related tools.

    Args:
        loader: WorkflowLoader instance
        state_manager: WorkflowStateManager instance (created from db if not provided)
        session_manager: LocalSessionManager instance (created from db if not provided)
        db: Database instance for creating default managers

    Returns:
        InternalToolRegistry with workflow tools registered

    Note:
        If db is None and state_manager/session_manager are not provided,
        tools requiring database access will return errors when called.
    """
    _db = db
    _loader = loader or WorkflowLoader(db=_db)

    # Create default managers only if db is provided
    if state_manager is not None:
        _state_manager = state_manager
    elif _db is not None:
        _state_manager = WorkflowStateManager(_db)
    else:
        _state_manager = None

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
        name="activate_workflow",
        description="Activate a step-based workflow for the current session. Accepts #N, N, UUID, or prefix for session_id.",
    )
    async def _activate_workflow(
        name: str,
        session_id: str | None = None,
        initial_step: str | None = None,
        variables: dict[str, Any] | None = None,
        project_path: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        if _state_manager is None or _session_manager is None or _db is None:
            return {"error": "Workflow tools require database connection"}
        return await activate_workflow(
            _loader,
            _state_manager,
            _session_manager,
            _db,
            name,
            session_id,
            initial_step,
            variables,
            project_path,
            resume,
            instance_manager=_instance_manager,
            session_var_manager=_session_var_manager,
        )

    @registry.tool(
        name="end_workflow",
        description="End a step-based workflow. Specify workflow name or defaults to current. Accepts #N, N, UUID, or prefix for session_id.",
    )
    async def _end_workflow(
        session_id: str | None = None,
        reason: str | None = None,
        project_path: str | None = None,
        workflow: str | None = None,
    ) -> dict[str, Any]:
        if _state_manager is None or _session_manager is None:
            return {"error": "Workflow tools require database connection"}
        return await end_workflow(
            _loader,
            _state_manager,
            _session_manager,
            session_id,
            reason,
            project_path,
            workflow=workflow,
            instance_manager=_instance_manager,
        )

    @registry.tool(
        name="get_workflow_status",
        description="Get workflow status for a session. Shows all active workflow instances and session variables. Accepts #N, N, UUID, or prefix for session_id.",
    )
    def _get_workflow_status(session_id: str | None = None) -> dict[str, Any]:
        if _state_manager is None or _session_manager is None:
            return {"error": "Workflow tools require database connection"}
        return get_workflow_status(
            _state_manager,
            _session_manager,
            session_id,
            instance_manager=_instance_manager,
            session_var_manager=_session_var_manager,
        )

    @registry.tool(
        name="request_step_transition",
        description="Request transition to a different step. Accepts #N, N, UUID, or prefix for session_id.",
    )
    async def _request_step_transition(
        to_step: str,
        reason: str | None = None,
        session_id: str | None = None,
        force: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        if _state_manager is None or _session_manager is None:
            return {"error": "Workflow tools require database connection"}
        return await request_step_transition(
            _loader,
            _state_manager,
            _session_manager,
            to_step,
            reason,
            session_id,
            force,
            project_path,
        )

    @registry.tool(
        name="set_variable",
        description="Set a variable scoped to a workflow instance or session. Use workflow param for workflow-scoped. Accepts #N, N, UUID, or prefix for session_id.",
    )
    def _set_variable(
        name: str,
        value: str | int | float | bool | None,
        session_id: str | None = None,
        workflow: str | None = None,
    ) -> dict[str, Any]:
        if _state_manager is None or _session_manager is None or _db is None:
            return {"error": "Workflow tools require database connection"}
        return set_variable(
            _state_manager,
            _session_manager,
            _db,
            name,
            value,
            session_id,
            workflow=workflow,
            instance_manager=_instance_manager,
            session_var_manager=_session_var_manager,
        )

    @registry.tool(
        name="get_variable",
        description="Get variable(s) scoped to a workflow instance or session. Use workflow param for workflow-scoped. Accepts #N, N, UUID, or prefix for session_id.",
    )
    def _get_variable(
        name: str | None = None,
        session_id: str | None = None,
        workflow: str | None = None,
    ) -> dict[str, Any]:
        if _state_manager is None or _session_manager is None or _db is None:
            return {"error": "Workflow tools require database connection"}
        return get_variable(
            _state_manager,
            _session_manager,
            _db,
            name,
            session_id,
            workflow=workflow,
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

    return registry
