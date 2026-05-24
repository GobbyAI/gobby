"""Factory for creating the spawn_agent MCP tool registry.

Loads agent definitions from workflow_definitions (DB-backed AgentDefinitionBody)
and delegates to spawn_agent_impl for execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from gobby.agents.completion_subscribers import subscribe_agent_completion
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.utils.project_context import get_project_context
from gobby.workflows.definitions import AgentDefinitionBody

from ._implementation import spawn_agent_impl

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


def _non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _first_string(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _non_empty_string(mapping.get(key))
        if value:
            return value
    return None


def _coalesce_string(mapping: dict[str, Any], key: str, fallback: str | None) -> str | None:
    return _non_empty_string(mapping.get(key)) or fallback


def _coalesce_bool(mapping: dict[str, Any], key: str, fallback: bool | None) -> bool | None:
    value = mapping.get(key)
    if isinstance(value, bool):
        return value
    return fallback


def _coalesce_number(mapping: dict[str, Any], key: str, fallback: float | None) -> float | None:
    value = mapping.get(key)
    if isinstance(value, int | float):
        return float(value)
    return fallback


def _suggestion_task_description(
    task_manager: LocalTaskManager | None,
    task_id: str | None,
) -> str:
    if not task_manager or not task_id:
        return ""
    try:
        full_task = task_manager.get_task(task_id)
    except Exception:
        logger.debug("Failed to load dispatch suggestion task %s", task_id, exc_info=True)
        return ""
    description = getattr(full_task, "description", "") if full_task else ""
    return description if isinstance(description, str) else ""


def _load_agent_body(
    name: str,
    db: HubDatabase | None,
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
    db: HubDatabase,
) -> str:
    """Register a synthetic WorkflowDefinition from agent's inline steps.

    Creates or updates a workflow definition in the DB that the step enforcement
    engine can look up via WorkflowInstance.workflow_name.

    Returns the workflow name.
    """
    from gobby.agents.step_workflow import register_agent_step_workflow

    return register_agent_step_workflow(agent_body, db)


def create_spawn_agent_registry(
    runner: AgentRunner,
    task_manager: LocalTaskManager | None = None,
    worktree_storage: Any | None = None,
    git_manager: Any | None = None,
    clone_storage: Any | None = None,
    clone_manager: Any | None = None,
    session_manager: Any | None = None,
    db: HubDatabase | None = None,
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
            subscribe_agent_completion(
                completion_registry=completion_registry,
                run_id=str(run_id),
                subscriber_session_id=resolved_parent_session_id,
                session_manager=session_manager,
                db=db,
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
        agent: str = "backend-developer",
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
            agent: Agent definition name (default: "backend-developer")
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
            if not isinstance(suggestion, dict):
                return {
                    "task_ref": "",
                    "run_id": "",
                    "success": False,
                    "error": "dispatch_batch suggestions must be objects",
                }

            task_id = _first_string(suggestion, "task_id", "id")
            task_ref = _first_string(suggestion, "ref", "task_ref", "task_id", "id")
            if not task_ref:
                return {
                    "task_ref": "",
                    "run_id": "",
                    "success": False,
                    "error": (
                        "dispatch_batch suggestion is missing ref, task_ref, task_id, or id; "
                        "refusing to spawn an unknown task"
                    ),
                }
            if not task_id:
                task_id = task_ref

            task_title = _first_string(suggestion, "title", "summary") or ""
            prompt = _non_empty_string(suggestion.get("prompt"))
            if prompt is None:
                if not task_title:
                    return {
                        "task_ref": task_ref,
                        "run_id": "",
                        "success": False,
                        "error": (
                            "dispatch_batch suggestion is missing prompt and title; "
                            f"refusing to spawn {task_ref}"
                        ),
                    }
                task_desc = _suggestion_task_description(task_manager, task_id)
                desc_block = f"\n\nDescription:\n{task_desc}" if task_desc else ""
                prompt = f"Implement task {task_ref}: {task_title}{desc_block}"

            suggestion_agent = _coalesce_string(suggestion, "agent", agent)
            try:
                result = await spawn_agent(
                    prompt=prompt,
                    agent=suggestion_agent or "backend-developer",
                    task_id=task_id,
                    worktree_id=_coalesce_string(suggestion, "worktree_id", worktree_id),
                    clone_id=_coalesce_string(suggestion, "clone_id", clone_id),
                    isolation=_coalesce_string(suggestion, "isolation", isolation),
                    branch_name=_coalesce_string(suggestion, "branch_name", branch_name),
                    base_branch=_coalesce_string(suggestion, "base_branch", base_branch),
                    provider=_coalesce_string(suggestion, "provider", provider),
                    model=_coalesce_string(suggestion, "model", model),
                    reasoning_effort=_coalesce_string(
                        suggestion, "reasoning_effort", reasoning_effort
                    ),
                    reasoning_required=_coalesce_bool(
                        suggestion, "reasoning_required", reasoning_required
                    ),
                    timeout=_coalesce_number(suggestion, "timeout", timeout),
                    parent_session_id=_coalesce_string(
                        suggestion, "parent_session_id", parent_session_id
                    ),
                )
                out: dict[str, Any] = {
                    "task_ref": task_ref,
                    "run_id": result.get("run_id", ""),
                    "success": result.get("success", False),
                    "agent": suggestion_agent or "backend-developer",
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
