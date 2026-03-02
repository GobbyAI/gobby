"""Factory for creating the spawn_agent MCP tool registry.

Loads agent definitions from workflow_definitions (DB-backed AgentDefinitionBody)
and delegates to spawn_agent_impl for execution.
"""

from __future__ import annotations

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
    """Load an agent definition from workflow_definitions, applying extends chain.

    Args:
        name: Agent name to look up.
        db: Database connection.
        project_id: Optional project id for scoped agents.

    Returns:
        AgentDefinitionBody if found and resolved cleanly, None otherwise.
    """
    if db is None:
        return None

    from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

    try:
        return resolve_agent(name, db, project_id=project_id)
    except AgentResolutionError as e:
        logger.error(f"Agent resolution failed: {e}")
        return None


def create_spawn_agent_registry(
    runner: AgentRunner,
    task_manager: LocalTaskManager | None = None,
    worktree_storage: Any | None = None,
    git_manager: Any | None = None,
    clone_storage: Any | None = None,
    clone_manager: Any | None = None,
    session_manager: Any | None = None,
    # For mode=self (workflow activation on caller session)
    state_manager: Any | None = None,  # WorkflowStateManager
    db: DatabaseProtocol | None = None,
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
        state_manager: WorkflowStateManager for mode=self activation.
        db: Database instance for agent lookups and mode=self activation.

    Returns:
        InternalToolRegistry with spawn_agent tool registered.
    """

    def _resolve_session_id(ref: str) -> str:
        """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
        if session_manager is None:
            return ref  # No resolution available, return as-is
        ctx = get_project_context()
        project_id = ctx.get("id") if ctx else None
        return str(session_manager.resolve_session_reference(ref, project_id))

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
        mode: Literal["terminal", "autonomous", "self"] | None = None,
        initial_step: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        # Limits
        timeout: float | None = None,
        max_turns: int | None = None,
        # Sandbox
        sandbox: bool | None = None,
        sandbox_mode: Literal["permissive", "restrictive"] | None = None,
        sandbox_allow_network: bool | None = None,
        sandbox_extra_paths: list[str] | None = None,
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
            mode: Execution mode (terminal/embedded/headless/self).
                  'self' activates workflow on caller session instead of spawning.
            initial_step: For mode=self, start at specific step (defaults to first)
            provider: AI provider (claude/gemini/codex/cursor/windsurf/copilot)
            model: Model to use
            timeout: Timeout in seconds
            max_turns: Maximum conversation turns
            sandbox: Enable sandbox (True/False/None)
            sandbox_mode: Sandbox mode (permissive/restrictive)
            sandbox_allow_network: Allow network access
            sandbox_extra_paths: Extra paths for sandbox write access
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

        # Compose prompt with preamble from agent definition
        effective_prompt = prompt
        if agent_body:
            preamble = agent_body.build_prompt_preamble()
            if preamble:
                effective_prompt = f"{preamble}\n\n---\n\n{prompt}"

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
                logger.warning("Workflow %r not found for agent spawn", effective_workflow)

        # Delegate to spawn_agent_impl
        return await spawn_agent_impl(
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
            mode=mode,
            initial_step=initial_step,
            provider=provider,
            model=model,
            timeout=timeout,
            max_turns=max_turns,
            sandbox=sandbox,
            sandbox_mode=sandbox_mode,
            sandbox_allow_network=sandbox_allow_network,
            sandbox_extra_paths=sandbox_extra_paths,
            parent_session_id=resolved_parent_session_id,
            project_path=project_path,
            initial_variables=initial_variables,
            # For mode=self
            state_manager=state_manager,
            session_manager=session_manager,
            db=db,
        )

    return registry
