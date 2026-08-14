"""
Internal MCP tools for Gobby Workflow System.

Umbrella server for workflows, pipelines, rules, variables, and agent definitions.

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.workflows._agents import (
    create_agent_definition,
    delete_agent_definition,
    get_agent_definition,
    list_agent_definitions,
    toggle_agent_definition,
    update_agent_rules,
    update_agent_step_workflow,
    update_agent_variables,
)
from gobby.mcp_proxy.tools.workflows._import import reload_cache
from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools
from gobby.mcp_proxy.tools.workflows._query import get_step_status
from gobby.mcp_proxy.tools.workflows._rules import (
    create_rule,
    delete_rule,
    get_rule,
    list_rules,
    toggle_rule,
    update_rule,
)
from gobby.mcp_proxy.tools.workflows._variables import (
    create_variable,
    delete_variable,
    export_variable,
    get_variable_definition,
    list_variables,
    update_variable,
)
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.project_context import get_project_context, get_workflow_project_path
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager

__all__ = [
    "create_workflows_registry",
    "get_workflow_project_path",
]

logger = logging.getLogger(__name__)


class _InternalToolRegistryInventory(Protocol):
    name: str

    def list_tools(self) -> Sequence[Mapping[str, Any]]:
        """Return registered internal tools."""
        ...


class _InternalRegistryInventory(Protocol):
    def list_servers(self) -> Sequence[Mapping[str, Any]]:
        """Return internal MCP server summaries."""
        ...

    def get_all_registries(self) -> Sequence[_InternalToolRegistryInventory]:
        """Return internal MCP registries."""
        ...


class _ExternalMCPInventory(Protocol):
    def get_available_servers(self) -> list[str]:
        """Return external MCP server names."""
        ...

    async def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        """Return external MCP tool inventory."""
        ...


def create_workflows_registry(
    loader: PipelineLoader | None = None,
    session_manager: SessionManager | None = None,
    db: HubDatabase | None = None,
    internal_manager: _InternalRegistryInventory | None = None,
    mcp_manager_resolver: Callable[[], _ExternalMCPInventory | None] | None = None,
    # Pipeline dependencies (resolved lazily at call time)
    executor_getter: Callable[[], Any | None] | None = None,
    execution_manager_getter: Callable[[], Any | None] | None = None,
    completion_registry: Any | None = None,
    detection_registry: DetectionManifestRegistry | None = None,
) -> InternalToolRegistry:
    """
    Create a workflow tool registry with all workflow-related tools.

    This is the umbrella registry for workflows, pipelines, rules,
    variables, and agent definitions.

    Args:
        loader: PipelineLoader instance
        session_manager: SessionManager instance (created from db if not provided)
        db: Database instance for creating default managers
        internal_manager: Internal registry inventory for semantic MCP checks
        mcp_manager_resolver: per-call resolver for the external MCP manager (optional)
        executor_getter: Callable returning PipelineExecutor (or None) at call time
        execution_manager_getter: Callable returning LocalPipelineExecutionManager
        completion_registry: CompletionEventRegistry for pipeline auto-subscriptions

    Returns:
        InternalToolRegistry with workflow, pipeline, rule, and agent definition tools
    """
    _db = db
    _loader = loader or PipelineLoader(db=_db)

    if session_manager is not None:
        _session_manager = session_manager
    elif _db is not None:
        _session_manager = SessionManager(_db)
    else:
        _session_manager = None

    # Create multi-workflow managers
    _instance_manager = AgentStepInstanceManager(_db) if _db is not None else None
    _session_var_manager = SessionVariableManager(_db) if _db is not None else None
    _rule_manager = RuleDefinitionManager(_db) if _db is not None else None
    _variable_manager = SessionVariableDefaultManager(_db) if _db is not None else None
    _agent_manager = AgentDefinitionManager(_db) if _db is not None else None

    registry = InternalToolRegistry(
        name="gobby-workflows",
        description="Workflow management - list, activate, status, transition, end",
    )

    @registry.tool(
        name="get_step_status",
        description=(
            "Get agent-step status for the current session. "
            "Shows the snapshot step list and session variables."
        ),
    )
    def _get_step_status(session_id: str | None = None) -> dict[str, Any]:
        if _session_manager is None:
            return {"error": "Workflow tools require database connection"}
        from gobby.utils.session_context import get_current_session_id

        effective_session_id = session_id or get_current_session_id()
        return get_step_status(
            _session_manager,
            effective_session_id,
            instance_manager=_instance_manager,
            session_var_manager=_session_var_manager,
        )

    @registry.tool(
        name="evaluate_pipeline",
        description="Validate a pipeline definition — structural checks without executing.",
    )
    async def _evaluate_pipeline(name: str) -> dict[str, Any]:
        from gobby.workflows.dry_run import evaluate_pipeline_definition

        mcp_inventory = workflow_mcp_inventory(internal_manager, mcp_manager_resolver)
        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None
        eval_result = await evaluate_pipeline_definition(
            name,
            _loader,
            project_id,
            mcp_inventory,
        )
        return eval_result.to_dict()

    @registry.tool(
        name="evaluate_agent",
        description=(
            "Validate an agent definition — tool gates plus inline step workflow, "
            "without executing."
        ),
    )
    async def _evaluate_agent(name: str) -> dict[str, Any]:
        from gobby.workflows.agent_resolver import resolve_agent
        from gobby.workflows.dry_run import (
            EvaluationItem,
            WorkflowEvaluation,
            evaluate_agent_definition,
        )

        if _db is None:
            return {"error": "Agent evaluation requires a database connection"}
        mcp_inventory = workflow_mcp_inventory(internal_manager, mcp_manager_resolver)
        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None
        agent = resolve_agent(name, _db, project_id=project_id)
        if agent is None:
            missing = WorkflowEvaluation(valid=False, workflow_name=name)
            missing.items.append(
                EvaluationItem(
                    layer="structure",
                    level="error",
                    code="AGENT_NOT_FOUND",
                    message=f"Agent '{name}' not found",
                )
            )
            return missing.to_dict()
        result = await evaluate_agent_definition(agent, mcp_inventory)
        return result.to_dict()

    @registry.tool(
        name="reload_cache",
        description=(
            "Clear the pipeline cache and re-sync imported and bundled definitions to DB. "
            "Use this after modifying YAML files."
        ),
    )
    def _reload_cache(
        project_path: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return reload_cache(
            _loader,
            db=_db,
            project_path=project_path,
            project_id=project_id,
            detection_registry=detection_registry,
        )

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
        if _rule_manager is None:
            return {"error": "Rule tools require database connection"}
        return list_rules(_rule_manager, event, group, enabled, brief=brief)

    @registry.tool(
        name="get_rule",
        description="Get full details of a standalone rule by name.",
    )
    def _get_rule(name: str) -> dict[str, Any]:
        if _rule_manager is None:
            return {"error": "Rule tools require database connection"}
        return get_rule(_rule_manager, name)

    @registry.tool(
        name="toggle_rule",
        description="Enable or disable a standalone rule by name.",
    )
    def _toggle_rule(name: str, enabled: bool) -> dict[str, Any]:
        if _rule_manager is None:
            return {"error": "Rule tools require database connection"}
        return toggle_rule(_rule_manager, name, enabled)

    @registry.tool(
        name="create_rule",
        description="Create a new standalone rule. Validates definition with RuleDefinitionBody before inserting.",
    )
    def _create_rule(
        name: str,
        definition: dict[str, Any],
        project_path: str | None = None,
        make_template: bool = False,
    ) -> dict[str, Any]:
        if _rule_manager is None:
            return {"error": "Rule tools require database connection"}
        pp = Path(project_path) if project_path else None
        return create_rule(
            _rule_manager, name, definition, project_path=pp, make_global_template=make_template
        )

    @registry.tool(
        name="delete_rule",
        description="Delete a standalone rule by name (soft-delete). Bundled rules are protected unless force=True.",
    )
    def _delete_rule(
        name: str,
        force: bool = False,
    ) -> dict[str, Any]:
        if _rule_manager is None:
            return {"error": "Rule tools require database connection"}
        return delete_rule(_rule_manager, name, force)

    @registry.tool(
        name="update_rule",
        description=(
            "Update fields on an existing standalone rule. Pass any subset of "
            "definition, description, enabled, priority, tags. When 'definition' "
            "is provided it replaces the rule body and is validated with "
            "RuleDefinitionBody."
        ),
    )
    def _update_rule(
        name: str,
        definition: dict[str, Any] | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
        project_path: str | None = None,
        make_template: bool = False,
    ) -> dict[str, Any]:
        if _rule_manager is None:
            return {"error": "Rule tools require database connection"}
        pp = Path(project_path) if project_path else None
        return update_rule(
            _rule_manager,
            name,
            definition=definition,
            description=description,
            enabled=enabled,
            priority=priority,
            tags=tags,
            project_path=pp,
            make_global_template=make_template,
        )

    # ── Variable definition CRUD tools ──

    @registry.tool(
        name="list_variables",
        description="List variable definitions. Supports filtering by enabled status.",
    )
    def _list_variables(
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        if _variable_manager is None:
            return {"error": "Variable tools require database connection"}
        return list_variables(_variable_manager, enabled)

    @registry.tool(
        name="get_variable_definition",
        description="Get a variable definition by name. Returns the definition details including default value.",
    )
    def _get_variable_definition(name: str) -> dict[str, Any]:
        if _variable_manager is None:
            return {"error": "Variable tools require database connection"}
        return get_variable_definition(_variable_manager, name)

    @registry.tool(
        name="create_variable",
        description="Create a new variable definition. Validates with VariableDefinitionBody before inserting.",
    )
    def _create_variable(
        name: str,
        value: Any,
        description: str | None = None,
        project_path: str | None = None,
        make_template: bool = False,
    ) -> dict[str, Any]:
        if _variable_manager is None:
            return {"error": "Variable tools require database connection"}
        pp = Path(project_path) if project_path else None
        return create_variable(
            _variable_manager,
            name,
            value,
            description,
            project_path=pp,
            make_global_template=make_template,
        )

    @registry.tool(
        name="update_variable",
        description="Update a variable definition's value or description by name.",
    )
    def _update_variable(
        name: str,
        value: Any = None,
        description: str | None = None,
        project_path: str | None = None,
        make_template: bool = False,
    ) -> dict[str, Any]:
        if _variable_manager is None:
            return {"error": "Variable tools require database connection"}
        pp = Path(project_path) if project_path else None
        return update_variable(
            _variable_manager,
            name,
            value,
            description,
            project_path=pp,
            make_global_template=make_template,
        )

    @registry.tool(
        name="delete_variable",
        description="Delete a variable definition by name (soft-delete). Bundled variables are protected unless force=True.",
    )
    def _delete_variable(
        name: str,
        force: bool = False,
    ) -> dict[str, Any]:
        if _variable_manager is None:
            return {"error": "Variable tools require database connection"}
        return delete_variable(_variable_manager, name, force)

    @registry.tool(
        name="export_variable",
        description="Export a variable definition as YAML content.",
    )
    def _export_variable(name: str) -> dict[str, Any]:
        if _variable_manager is None:
            return {"error": "Variable tools require database connection"}
        return export_variable(_variable_manager, name)

    # ── Agent definition CRUD tools ──

    @registry.tool(
        name="list_agent_definitions",
        description="List agent definitions. Supports filtering by enabled status, project ID, and usage surface.",
    )
    def _list_agent_definitions(
        enabled: bool | None = None,
        project_id: str | None = None,
        surface_filter: str | None = None,
    ) -> dict[str, Any]:
        if _agent_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return list_agent_definitions(_agent_manager, enabled, project_id, surface_filter)

    @registry.tool(
        name="get_agent_definition",
        description="Get full details of an agent definition by name.",
    )
    def _get_agent_definition(name: str) -> dict[str, Any]:
        if _agent_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return get_agent_definition(_agent_manager, name)

    @registry.tool(
        name="create_agent_definition",
        description="Create a new agent definition. Validates with AgentDefinitionBody before inserting.",
    )
    def _create_agent_definition(
        name: str,
        definition: dict[str, Any],
        project_path: str | None = None,
        make_template: bool = False,
    ) -> dict[str, Any]:
        if _agent_manager is None:
            return {"error": "Agent definition tools require database connection"}
        pp = Path(project_path) if project_path else None
        return create_agent_definition(
            _agent_manager, name, definition, project_path=pp, make_global_template=make_template
        )

    @registry.tool(
        name="toggle_agent_definition",
        description="Enable or disable an agent definition by name.",
    )
    def _toggle_agent_definition(name: str, enabled: bool) -> dict[str, Any]:
        if _agent_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return toggle_agent_definition(_agent_manager, name, enabled)

    @registry.tool(
        name="delete_agent_definition",
        description="Delete an agent definition by name (soft-delete). Template agents are protected unless force=True.",
    )
    def _delete_agent_definition(
        name: str,
        force: bool = False,
    ) -> dict[str, Any]:
        if _agent_manager is None:
            return {"error": "Agent definition tools require database connection"}
        return delete_agent_definition(_agent_manager, name, force)

    @registry.tool(
        name="update_agent_rules",
        description="Add or remove rules from an agent definition's workflows.rules list.",
    )
    def _update_agent_rules(
        name: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        project_path: str | None = None,
        make_template: bool = False,
    ) -> dict[str, Any]:
        if _agent_manager is None:
            return {"error": "Agent definition tools require database connection"}
        pp = Path(project_path) if project_path else None
        return update_agent_rules(
            _agent_manager, name, add, remove, project_path=pp, make_global_template=make_template
        )

    @registry.tool(
        name="update_agent_variables",
        description="Set or remove variables from an agent definition's workflows.variables dict.",
    )
    def _update_agent_variables(
        name: str,
        set_vars: dict[str, Any] | None = None,
        remove: list[str] | None = None,
        project_path: str | None = None,
        make_template: bool = False,
    ) -> dict[str, Any]:
        if _agent_manager is None:
            return {"error": "Agent definition tools require database connection"}
        pp = Path(project_path) if project_path else None
        return update_agent_variables(
            _agent_manager,
            name,
            set_vars,
            remove,
            project_path=pp,
            make_global_template=make_template,
        )

    @registry.tool(
        name="update_agent_step_workflow",
        description="Replace an agent's nested step_workflow. Pass the object or None to clear.",
    )
    def _update_agent_step_workflow(
        name: str,
        step_workflow: dict[str, Any] | None = None,
        project_path: str | None = None,
        make_template: bool = False,
    ) -> dict[str, Any]:
        if _agent_manager is None:
            return {"error": "Agent definition tools require database connection"}
        pp = Path(project_path) if project_path else None
        return update_agent_step_workflow(
            _agent_manager,
            name,
            step_workflow,
            project_path=pp,
            make_global_template=make_template,
        )

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
    )

    return registry


def workflow_mcp_inventory(
    internal_manager: _InternalRegistryInventory | None,
    mcp_manager_resolver: Callable[[], _ExternalMCPInventory | None] | None,
) -> "_WorkflowMCPInventory | None":
    if internal_manager is None and mcp_manager_resolver is None:
        return None
    return _WorkflowMCPInventory(
        internal_manager=internal_manager, mcp_manager_resolver=mcp_manager_resolver
    )


class _WorkflowMCPInventory:
    """Combined internal and external MCP inventory for workflow semantic checks."""

    def __init__(
        self,
        *,
        internal_manager: _InternalRegistryInventory | None,
        mcp_manager_resolver: Callable[[], _ExternalMCPInventory | None] | None,
    ) -> None:
        self._internal_manager = internal_manager
        self._mcp_manager_resolver = mcp_manager_resolver

    def _current_mcp_manager(self) -> _ExternalMCPInventory | None:
        resolver = self._mcp_manager_resolver
        return resolver() if resolver is not None else None

    def get_available_servers(self) -> list[str]:
        servers = set(self._internal_server_names())
        mcp_manager = self._current_mcp_manager()
        if mcp_manager is not None:
            servers.update(mcp_manager.get_available_servers())
        return sorted(servers)

    async def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        tools: dict[str, list[dict[str, Any]]] = {}
        mcp_manager = self._current_mcp_manager()
        if mcp_manager is not None:
            tools.update(await mcp_manager.list_tools())
        internal_tools = self._internal_tools()
        collisions = sorted(set(tools) & set(internal_tools))
        if collisions:
            logger.warning(
                "Internal workflow MCP tools override external server keys: %s",
                ", ".join(collisions),
                extra={
                    "collision_count": len(collisions),
                    "collisions": collisions,
                    "external_count": len(tools),
                    "internal_count": len(internal_tools),
                },
            )
        tools.update(internal_tools)
        return tools

    def _internal_server_names(self) -> list[str]:
        if self._internal_manager is None:
            return []
        return [
            str(server["name"])
            for server in self._internal_manager.list_servers()
            if isinstance(server, dict) and server.get("name")
        ]

    def _internal_tools(self) -> dict[str, list[dict[str, Any]]]:
        if self._internal_manager is None:
            return {}
        return {
            registry.name: [dict(tool) for tool in registry.list_tools()]
            for registry in self._internal_manager.get_all_registries()
        }
