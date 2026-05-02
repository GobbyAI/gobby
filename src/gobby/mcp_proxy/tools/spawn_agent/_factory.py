"""Factory for creating the spawn_agent MCP tool registry.

Loads agent definitions from workflow_definitions (DB-backed AgentDefinitionBody)
and delegates to spawn_agent_impl for execution.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.utils.project_context import get_project_context
from gobby.workflows.definitions import AgentDefinitionBody

from ._implementation import spawn_agent_impl

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


def _load_agent_body(
    name: str,
    db: DatabaseProtocol | None,
    project_id: str | None = None,
) -> AgentDefinitionBody | None:
    """Load an agent definition from workflow_definitions via direct lookup.

    Args:
        name: Agent name to look up.
        db: Database connection.
        project_id: Optional project id for scoped agents.

    Returns:
        AgentDefinitionBody if found, None otherwise.
    """
    if db is None:
        return None

    from gobby.workflows.agent_resolver import resolve_agent

    return resolve_agent(name, db, project_id=project_id)


def _register_agent_step_workflow(
    agent_body: AgentDefinitionBody,
    db: DatabaseProtocol,
) -> str:
    """Register a synthetic WorkflowDefinition from agent's inline steps.

    Creates or updates a workflow definition in the DB that the step enforcement
    engine can look up via WorkflowInstance.workflow_name.

    Returns the workflow name.
    """
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

    step_workflow_name = f"{agent_body.name}-steps"
    def_manager = LocalWorkflowDefinitionManager(db)

    wf_data = {
        "name": step_workflow_name,
        "description": f"Auto-generated step workflow for {agent_body.name} agent",
        "type": "step",
        "version": "2.0",
        "enabled": False,
        "steps": [step.model_dump() for step in (agent_body.steps or [])],
        "variables": agent_body.step_variables,
        "exit_condition": agent_body.exit_condition,
    }
    definition_json = json.dumps(wf_data)

    existing = def_manager.get_by_name(step_workflow_name)
    if existing:
        def_manager.update(
            existing.id,
            definition_json=definition_json,
            workflow_type="workflow",
            source="agent",
        )
    else:
        def_manager.create(
            name=step_workflow_name,
            definition_json=definition_json,
            workflow_type="workflow",
            enabled=False,
            source="agent",
        )

    return step_workflow_name


def create_spawn_agent_registry(
    runner: AgentRunner,
    task_manager: LocalTaskManager | None = None,
    worktree_storage: Any | None = None,
    git_manager: Any | None = None,
    clone_storage: Any | None = None,
    clone_manager: Any | None = None,
    session_manager: Any | None = None,
    db: DatabaseProtocol | None = None,
    completion_registry: Any | None = None,
    daemon_config: Any | None = None,
    code_index: Any | None = None,
) -> InternalToolRegistry:
    """
    Create a spawn_agent tool registry with the unified spawn_agent tool.

    Args:
        runner: AgentRunner instance for executing agents.
        task_manager: Task manager for task resolution.
        worktree_storage: Storage for worktree records.
        git_manager: Git manager for worktree operations.
        clone_storage: Storage for clone records.
        clone_manager: Git manager for clone operations.
        session_manager: Session manager for resolving session references.
        db: Database instance for agent lookups.

    Returns:
        InternalToolRegistry with spawn_agent tool registered.
    """

    from gobby.utils.session_context import resolve_session_ref

    def _resolve_session_id(ref: str) -> str:
        return resolve_session_ref(session_manager, ref)

    registry = InternalToolRegistry(
        name="gobby-spawn-agent",
        description="Unified agent spawning with isolation support",
    )

    @registry.tool(
        name="spawn_agent",
        description=(
            "Spawn a subagent to execute a task. Supports isolation modes: "
            "'none' (work in current directory), 'worktree' (create git worktree), "
            "'clone' (create shallow clone). Can use named agent definitions or raw parameters. "
            "Accepts #N, N, UUID, or prefix for parent_session_id."
        ),
    )
    async def spawn_agent(
        prompt: str,
        agent: str = "default",
        task_id: str | None = None,
        # Isolation
        isolation: Literal["none", "worktree", "clone"] | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        clone_id: str | None = None,
        worktree_id: str | None = None,
        # Execution
        workflow: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reasoning_required: bool | None = None,
        # Limits
        timeout: float | None = None,
        max_turns: int | None = None,
        # Context
        parent_session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Spawn a subagent with the specified configuration.

        Args:
            prompt: Required - what the agent should do
            agent: Agent definition name (defaults to "default")
            task_id: Optional - link to task (supports N, #N, UUID)
            isolation: Isolation mode (none/worktree/clone)
            branch_name: Git branch name (auto-generated from task if not provided)
            base_branch: Base branch for worktree/clone
            clone_id: Existing clone ID to reuse
            worktree_id: Existing worktree ID to reuse
            workflow: Workflow/pipeline to use
            provider: AI provider (claude/gemini/qwen/codex/droid)
            model: Model to use
            reasoning_effort: Optional reasoning override for supported providers/models
            reasoning_required: Fail instead of warning when the requested reasoning is unsupported
            timeout: Timeout in seconds
            max_turns: Maximum conversation turns
            parent_session_id: Session reference (accepts #N, N, UUID, or prefix) for the parent session
            project_path: Project path override

        Returns:
            Dict with success status, run_id, child_session_id, isolation metadata
        """
        # Resolve parent_session_id to UUID (accepts #N, N, UUID, or prefix)
        resolved_parent_session_id = parent_session_id
        if parent_session_id:
            try:
                resolved_parent_session_id = _resolve_session_id(parent_session_id)
            except ValueError as e:
                return {"success": False, "error": str(e)}

        # Load agent definition body from DB
        ctx = get_project_context()
        project_id = ctx.get("id") if ctx else None
        agent_body = _load_agent_body(agent, db, project_id=project_id)
        if agent_body is None and agent != "default":
            return {"success": False, "error": f"Agent '{agent}' not found"}

        # Compose prompt — hooks inject agent instructions via session_start,
        # so no preamble prepend needed here.
        effective_prompt = prompt

        # Determine effective workflow
        # Agent's pipeline (if set) is the default; explicit param overrides
        effective_workflow = workflow
        if effective_workflow is None and agent_body and agent_body.workflows.pipeline:
            effective_workflow = agent_body.workflows.pipeline

        # Build initial_variables for rule activation
        initial_variables: dict[str, Any] = {}
        if agent_body:
            initial_variables["_agent_type"] = agent_body.name
            if agent_body.workflows.rules:
                initial_variables["_agent_rules"] = agent_body.workflows.rules
            if agent_body.workflows.variables:
                initial_variables.update(agent_body.workflows.variables)

        # Auto-register inline step workflow if agent has steps
        if agent_body and agent_body.steps and db:
            step_wf_name = _register_agent_step_workflow(agent_body, db)
            initial_variables["_step_workflow_name"] = step_wf_name

        # Inject _assigned_pipeline if the workflow is a PipelineDefinition
        if effective_workflow:
            from gobby.workflows.loader import WorkflowLoader

            wf_loader = WorkflowLoader(db=db)
            wf_def = wf_loader.load_workflow_sync(effective_workflow, project_path=project_path)
            if wf_def:
                from gobby.workflows.definitions import PipelineDefinition

                if isinstance(wf_def, PipelineDefinition):
                    initial_variables["_assigned_pipeline"] = effective_workflow
            else:
                logger.warning(f"Workflow {effective_workflow!r} not found for agent spawn")

        # Fallback agent: if this agent's provider has failed on this task,
        # walk the fallback chain to find a viable agent definition.
        if task_id and db and agent_body and agent_body.fallback_agent and not provider:
            try:
                from gobby.agents.provider_rotation import get_failed_providers_for_task
                from gobby.storage.agents import LocalAgentRunManager

                arm = LocalAgentRunManager(db)
                failed_providers = get_failed_providers_for_task(task_id, arm)

                def _resolve_provider(p: str | None) -> str:
                    if p is None or p == "inherit":
                        return "claude"
                    return p

                agent_provider = _resolve_provider(agent_body.provider)

                if agent_provider in failed_providers:
                    visited: set[str] = {agent_body.name}
                    candidate_name: str | None = agent_body.fallback_agent
                    fallback_body = None
                    max_depth = 5

                    for _ in range(max_depth):
                        if not candidate_name or candidate_name in visited:
                            if candidate_name in visited:
                                logger.warning(
                                    f"Cycle detected in fallback chain: "
                                    f"{candidate_name!r} already visited {visited}"
                                )
                            break
                        visited.add(candidate_name)
                        candidate = _load_agent_body(candidate_name, db, project_id=project_id)
                        if not candidate:
                            break
                        candidate_provider = _resolve_provider(candidate.provider)
                        if candidate_provider not in failed_providers:
                            fallback_body = candidate
                            break
                        candidate_name = candidate.fallback_agent

                    if fallback_body:
                        logger.info(
                            f"Provider {agent_provider} failed for task {task_id}, "
                            f"falling back from {agent_body.name} to {fallback_body.name}"
                        )
                        agent_body = fallback_body
                        agent = agent_body.name
            except Exception as e:
                logger.debug(f"Fallback agent check failed: {e}")

        # Delegate to spawn_agent_impl
        result = await spawn_agent_impl(
            prompt=effective_prompt,
            runner=runner,
            agent_body=agent_body,
            agent_lookup_name=agent,
            task_id=task_id,
            task_manager=task_manager,
            isolation=isolation,
            branch_name=branch_name,
            base_branch=base_branch,
            clone_id=clone_id,
            worktree_id=worktree_id,
            worktree_storage=worktree_storage,
            git_manager=git_manager,
            clone_storage=clone_storage,
            clone_manager=clone_manager,
            workflow=effective_workflow,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            reasoning_required=reasoning_required,
            timeout=timeout,
            max_turns=max_turns,
            parent_session_id=resolved_parent_session_id,
            project_path=project_path,
            initial_variables=initial_variables,
            session_manager=session_manager,
            db=db,
            daemon_config=daemon_config,
            code_index=code_index,
        )

        # Auto-subscribe parent session + lineage to agent completion events
        run_id = result.get("run_id")
        if result.get("success") and run_id and completion_registry and resolved_parent_session_id:
            _auto_subscribe_agent(
                completion_registry,
                run_id,
                resolved_parent_session_id,
                session_manager,
                db,
            )

        return result

    @registry.tool(
        name="dispatch_batch",
        description=(
            "Dispatch multiple agents in parallel for non-conflicting tasks. "
            "Takes task briefs from suggest_next_task and spawns an agent for each. "
            "Uses asyncio.gather for concurrent spawning."
        ),
    )
    async def dispatch_batch(
        suggestions: list[dict[str, Any]],
        agent: str = "developer",
        worktree_id: str | None = None,
        clone_id: str | None = None,
        isolation: Literal["none", "worktree", "clone"] | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reasoning_required: bool | None = None,
        parent_session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Dispatch multiple agents for non-conflicting tasks.

        Args:
            suggestions: Task briefs from suggest_next_task output
            agent: Agent definition name (default: "developer")
            worktree_id: Shared worktree ID for all agents
            clone_id: Existing clone ID for all agents
            isolation: Isolation mode (none/worktree/clone)
            branch_name: Git branch name for isolation
            base_branch: Base branch for worktree/clone
            provider: AI provider override
            model: Model override
            parent_session_id: Parent session reference
            timeout: Timeout in seconds for each agent

        Returns:
            Dict with dispatched count and per-task results
        """
        import asyncio

        if not suggestions:
            return {"dispatched": 0, "results": []}

        async def _spawn_one(suggestion: dict[str, Any]) -> dict[str, Any]:
            task_ref = suggestion.get("ref", suggestion.get("id", "unknown"))
            task_title = suggestion.get("title", "")
            task_id = suggestion.get("id")
            # Fetch full description via task_manager (to_brief() intentionally omits it)
            task_desc = ""
            if task_id and task_manager:
                full_task = task_manager.get_task(task_id)
                if full_task and full_task.description:
                    task_desc = full_task.description
            desc_block = f"\n\nDescription:\n{task_desc}" if task_desc else ""
            try:
                result = await spawn_agent(
                    prompt=f"Implement task {task_ref}: {task_title}{desc_block}",
                    agent=agent,
                    task_id=task_id,
                    worktree_id=worktree_id,
                    clone_id=clone_id,
                    isolation=isolation,
                    branch_name=branch_name,
                    base_branch=base_branch,
                    provider=provider,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    reasoning_required=reasoning_required,
                    timeout=timeout,
                    parent_session_id=parent_session_id,
                )
                out: dict[str, Any] = {
                    "task_ref": task_ref,
                    "run_id": result.get("run_id", ""),
                    "success": result.get("success", False),
                }
                if not out["success"] and result.get("error"):
                    out["error"] = result["error"]
                return out
            except Exception as e:
                logger.error(f"Failed to spawn agent for {task_ref}: {e}")
                return {
                    "task_ref": task_ref,
                    "run_id": "",
                    "success": False,
                    "error": str(e),
                }

        results = await asyncio.gather(*[_spawn_one(s) for s in suggestions])
        dispatched = sum(1 for r in results if r["success"])

        return {
            "dispatched": dispatched,
            "results": list(results),
        }

    return registry


def _auto_subscribe_agent(
    completion_registry: Any,
    run_id: str,
    parent_session_id: str,
    session_manager: Any | None,
    db: Any | None,
) -> None:
    """Register a completion event for an agent run and subscribe parent lineage."""
    lineage_ids: list[str] = [parent_session_id]
    if session_manager:
        try:
            from gobby.agents.session import ChildSessionManager

            child_mgr = ChildSessionManager(session_manager)
            lineage = child_mgr.get_session_lineage(parent_session_id)
            lineage_ids = [s.id for s in lineage]
            if parent_session_id not in lineage_ids:
                lineage_ids.append(parent_session_id)
        except Exception:
            logger.debug(
                f"Could not resolve session lineage for {parent_session_id}", exc_info=True
            )

    try:
        completion_registry.register(run_id, subscribers=lineage_ids)
    except Exception:
        logger.debug(f"Failed to register completion event for run {run_id}", exc_info=True)
        return

    # Persist subscribers for restart recovery
    if db is not None:
        try:
            from gobby.storage.pipelines import LocalPipelineExecutionManager

            em = LocalPipelineExecutionManager(db=db, project_id="")
            em.add_completion_subscribers(run_id, lineage_ids)
        except Exception:
            logger.debug(
                f"Failed to persist completion subscribers for run {run_id}", exc_info=True
            )
