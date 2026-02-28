"""Core spawn_agent implementation.

Contains spawn_agent_impl() — the internal implementation used by both
the spawn_agent MCP tool and direct callers.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from gobby.agents.isolation import (
    SpawnConfig,
    get_isolation_handler,
)
from gobby.agents.registry import RunningAgent, get_running_agent_registry
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_executor import SpawnRequest, execute_spawn
from gobby.config.tmux import TmuxConfig
from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp
from gobby.utils.machine_id import get_machine_id
from gobby.utils.project_context import get_project_context
from gobby.workflows.definitions import AgentDefinitionBody

from ._health import TMUX_HEALTH_CHECK_DELAY, _check_tmux_session_alive, _health_check_tasks
from ._modes import _handle_self_mode, _handle_self_persona

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


async def spawn_agent_impl(
    prompt: str,
    runner: AgentRunner,
    agent_body: AgentDefinitionBody | None = None,
    agent_lookup_name: str | None = None,
    task_id: str | None = None,
    task_manager: LocalTaskManager | None = None,
    # Isolation
    isolation: Literal["none", "worktree", "clone"] | None = None,
    branch_name: str | None = None,
    base_branch: str | None = None,
    clone_id: str | None = None,  # Reuse existing clone instead of creating new isolation
    worktree_id: str | None = None,  # Reuse existing worktree instead of creating new isolation
    # Storage/managers for isolation
    worktree_storage: Any | None = None,
    git_manager: Any | None = None,
    clone_storage: Any | None = None,
    clone_manager: Any | None = None,
    # Execution
    workflow: str | None = None,
    mode: Literal["terminal", "embedded", "headless", "self"] | None = None,
    initial_step: str | None = None,  # For mode=self, start at specific step
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
    initial_variables: dict[str, Any] | None = None,
    # For mode=self (workflow activation on caller session)
    state_manager: Any | None = None,  # WorkflowStateManager
    session_manager: Any | None = None,  # LocalSessionManager
    db: Any | None = None,  # DatabaseProtocol
) -> dict[str, Any]:
    """
    Core spawn_agent implementation that can be called directly.

    Args:
        prompt: Required - what the agent should do (with preamble already applied)
        runner: AgentRunner instance for executing agents
        agent_body: Optional loaded agent definition body
        agent_lookup_name: The name used to look up the agent definition
        task_id: Optional - link to task (supports N, #N, UUID)
        task_manager: Task manager for task resolution
        isolation: Isolation mode (none/worktree/clone)
        branch_name: Git branch name (auto-generated from task if not provided)
        base_branch: Base branch for worktree/clone
        clone_id: Existing clone ID to reuse
        worktree_id: Existing worktree ID to reuse
        worktree_storage: Storage for worktree records
        git_manager: Git manager for worktree operations
        clone_storage: Storage for clone records
        clone_manager: Git manager for clone operations
        workflow: Workflow to use
        mode: Execution mode (terminal/embedded/headless/self)
        initial_step: For mode=self, start at specific step
        provider: AI provider (claude/gemini/codex/cursor/windsurf/copilot)
        model: Model to use
        timeout: Timeout in seconds
        max_turns: Maximum conversation turns
        sandbox: Enable sandbox (True/False/None)
        sandbox_mode: Sandbox mode (permissive/restrictive)
        sandbox_allow_network: Allow network access
        sandbox_extra_paths: Extra paths for sandbox write access
        parent_session_id: Parent session ID
        project_path: Project path override
        initial_variables: Pre-built initial variables from factory (merged with impl's own)
        state_manager: WorkflowStateManager for mode=self
        session_manager: LocalSessionManager for mode=self
        db: DatabaseProtocol for mode=self

    Returns:
        Dict with success status, run_id, child_session_id, isolation metadata
    """
    # 1. Merge config: agent_body defaults < params
    _raw_isolation: str | None = isolation
    if _raw_isolation is None and agent_body:
        _raw_isolation = agent_body.isolation
    if _raw_isolation in (None, "inherit"):
        _raw_isolation = "none"
    effective_isolation: Literal["none", "worktree", "clone"] = (
        _raw_isolation if _raw_isolation in ("none", "worktree", "clone") else "none"  # type: ignore[assignment]
    )

    effective_provider: str = provider or "claude"
    if effective_provider == "inherit":
        effective_provider = "claude"
    if effective_provider is None and agent_body:
        effective_provider = agent_body.provider
    if effective_provider in (None, "inherit"):
        effective_provider = "claude"

    _raw_mode: str | None = mode
    if _raw_mode is None and agent_body:
        _raw_mode = agent_body.mode
    if _raw_mode in (None, "inherit"):
        _raw_mode = "self"
    effective_mode: Literal["terminal", "embedded", "headless", "self"] = (
        _raw_mode if _raw_mode in ("terminal", "embedded", "headless", "self") else "self"  # type: ignore[assignment]
    )

    effective_model = model
    if effective_model is None and agent_body:
        effective_model = agent_body.model

    effective_timeout = timeout
    if effective_timeout is None and agent_body and agent_body.timeout:
        effective_timeout = agent_body.timeout
    if effective_timeout == 0:
        effective_timeout = None  # 0 means no timeout

    effective_workflow = workflow

    # Handle mode=self: activate workflow on caller session instead of spawning
    if effective_mode == "self":
        if effective_isolation != "none":
            logger.debug(f"mode=self overrides isolation={effective_isolation} to 'none'")
            effective_isolation = "none"
        if not effective_workflow and not agent_body:
            return {
                "success": False,
                "error": "mode: self requires a workflow to activate or an agent persona",
            }
        if not parent_session_id:
            return {
                "success": False,
                "error": "mode: self requires parent_session_id (the session to activate on)",
            }

        # Resolve initial_variables for workflow activation
        self_step_variables: dict[str, Any] = {}
        if initial_variables:
            self_step_variables.update(initial_variables)

        # Pass the agent lookup name so orchestrator workflows can spawn workers
        if agent_lookup_name:
            self_step_variables["agent_name"] = agent_lookup_name

        if task_id and task_manager:
            ctx = get_project_context(Path(project_path) if project_path else None)
            self_project_id = ctx.get("id") if ctx else None
            if self_project_id:
                try:
                    self_task_id = resolve_task_id_for_mcp(task_manager, task_id, self_project_id)
                    task = task_manager.get_task(self_task_id)
                    if task:
                        task_ref = f"#{task.seq_num}" if task.seq_num else self_task_id
                        self_step_variables["assigned_task_id"] = task_ref
                        self_step_variables["session_task"] = task_ref
                except Exception as e:
                    logger.warning(f"Failed to resolve task_id {task_id}: {e}")

        # Create WorkflowLoader on demand for mode=self
        from gobby.workflows.loader import WorkflowLoader

        workflow_loader = WorkflowLoader()

        if effective_workflow:
            return await _handle_self_mode(
                workflow=effective_workflow,
                parent_session_id=parent_session_id,
                step_variables=self_step_variables,
                initial_step=initial_step,
                workflow_loader=workflow_loader,
                state_manager=state_manager,
                session_manager=session_manager,
                db=db,
                project_path=project_path,
            )
        else:
            if agent_body is None:
                return {"success": False, "error": "Agent body is required for self-persona mode"}
            return await _handle_self_persona(
                agent_body=agent_body,
                agent_name=agent_lookup_name or agent_body.name,
                parent_session_id=parent_session_id,
                session_manager=session_manager,
                db=db,
            )

    effective_base_branch = base_branch
    if effective_base_branch is None and agent_body:
        effective_base_branch = agent_body.base_branch
    # Auto-detect current branch if no base_branch specified
    if effective_base_branch is None and git_manager:
        try:
            effective_base_branch = git_manager.get_current_branch()
        except Exception as e:
            logger.debug(f"Failed to auto-detect current branch: {e}", exc_info=True)
            effective_base_branch = None
    effective_base_branch = effective_base_branch or "main"

    # Build sandbox config from tool params (no agent_def.sandbox in simplified model)
    effective_sandbox_config: SandboxConfig | None = None

    sandbox_enabled = sandbox
    if sandbox_enabled is True or (
        sandbox_enabled is None
        and (sandbox_mode is not None or sandbox_allow_network is not None or sandbox_extra_paths)
    ):
        effective_sandbox_config = SandboxConfig(
            enabled=True,
            mode=sandbox_mode or "permissive",
            allow_network=sandbox_allow_network if sandbox_allow_network is not None else True,
            extra_write_paths=sandbox_extra_paths or [],
        )
    elif sandbox_enabled is False:
        effective_sandbox_config = SandboxConfig(enabled=False)

    # 2. Resolve project context
    ctx = get_project_context(Path(project_path) if project_path else None)
    if ctx is None:
        return {"success": False, "error": "Could not resolve project context"}

    project_id = ctx.get("id") or ctx.get("project_id")
    resolved_project_path = ctx.get("project_path")

    if not project_id or not isinstance(project_id, str):
        return {"success": False, "error": "Could not resolve project_id from context"}
    if not resolved_project_path or not isinstance(resolved_project_path, str):
        return {"success": False, "error": "Could not resolve project_path from context"}

    # 3. Validate parent_session_id and spawn depth
    if not parent_session_id:
        return {"success": False, "error": "parent_session_id is required"}

    can_spawn, reason, _depth = runner.can_spawn(parent_session_id)
    if not can_spawn:
        return {"success": False, "error": reason}

    # 4. Resolve task_id if provided (supports N, #N, UUID)
    resolved_task_id: str | None = None
    task_title: str | None = None
    task_seq_num: int | None = None

    if task_id and task_manager:
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id, project_id)
            task = task_manager.get_task(resolved_task_id)
            if task:
                task_title = task.title
                task_seq_num = task.seq_num
        except Exception as e:
            logger.warning(f"Failed to resolve task_id {task_id}: {e}")

    # 5. Handle worktree_id/clone_id reuse: skip isolation creation when existing resource provided
    isolation_ctx = None
    if worktree_id and worktree_storage:
        existing_worktree = worktree_storage.get(worktree_id)
        if not existing_worktree:
            return {"success": False, "error": f"Worktree {worktree_id} not found"}

        from gobby.agents.isolation import IsolationContext

        isolation_ctx = IsolationContext(
            cwd=existing_worktree.worktree_path,
            branch_name=existing_worktree.branch_name,
            worktree_id=existing_worktree.id,
            isolation_type="worktree",
            extra={"main_repo_path": resolved_project_path, "reused_worktree": True},
        )
        handler = get_isolation_handler("none")
    elif clone_id and clone_storage:
        existing_clone = clone_storage.get(clone_id)
        if not existing_clone:
            return {"success": False, "error": f"Clone {clone_id} not found"}

        from gobby.agents.isolation import IsolationContext

        isolation_ctx = IsolationContext(
            cwd=existing_clone.clone_path,
            branch_name=existing_clone.branch_name,
            clone_id=existing_clone.id,
            isolation_type="clone",
            extra={"source_repo": resolved_project_path, "reused_clone": True},
        )
        handler = get_isolation_handler("none")
    else:
        # Normal isolation flow
        handler = get_isolation_handler(
            effective_isolation,
            git_manager=git_manager,
            worktree_storage=worktree_storage,
            clone_manager=clone_manager,
            clone_storage=clone_storage,
        )

    # 6. Build spawn config
    spawn_config = SpawnConfig(
        prompt=prompt,
        task_id=resolved_task_id,
        task_title=task_title,
        task_seq_num=task_seq_num,
        branch_name=branch_name,
        branch_prefix=None,
        base_branch=effective_base_branch,
        project_id=project_id,
        project_path=resolved_project_path,
        provider=effective_provider,
        parent_session_id=parent_session_id,
    )

    # 7. Prepare environment (worktree/clone creation) — skipped if clone_id was reused
    if isolation_ctx is None:
        try:
            isolation_ctx = await handler.prepare_environment(spawn_config)
        except Exception as e:
            logger.error(f"Failed to prepare environment: {e}", exc_info=True)
            try:
                await handler.cleanup_environment(spawn_config)
            except Exception as cleanup_err:
                logger.warning(f"Cleanup after prepare failure also failed: {cleanup_err}")
            return {"success": False, "error": f"Failed to prepare environment: {e}"}

    # 7b. Add main repo path to sandbox read AND write paths for worktree isolation
    if (
        effective_isolation == "worktree"
        and effective_sandbox_config
        and effective_sandbox_config.enabled
        and isolation_ctx.extra.get("main_repo_path")
    ):
        main_repo_path = isolation_ctx.extra["main_repo_path"]
        main_repo_path_str = str(main_repo_path)
        existing_read_paths = list(effective_sandbox_config.extra_read_paths or [])
        existing_write_paths = list(effective_sandbox_config.extra_write_paths or [])
        paths_updated = False
        if main_repo_path_str not in existing_read_paths:
            existing_read_paths.append(main_repo_path_str)
            paths_updated = True
        if main_repo_path_str not in existing_write_paths:
            existing_write_paths.append(main_repo_path_str)
            paths_updated = True
        if paths_updated:
            effective_sandbox_config = SandboxConfig(
                enabled=effective_sandbox_config.enabled,
                mode=effective_sandbox_config.mode,
                allow_network=effective_sandbox_config.allow_network,
                extra_read_paths=existing_read_paths,
                extra_write_paths=existing_write_paths,
            )
            logger.debug(
                f"Added main repo path {main_repo_path} to sandbox read/write paths for worktree"
            )

    # 8. Build enhanced prompt with isolation context
    enhanced_prompt = handler.build_context_prompt(prompt, isolation_ctx)

    # 9. Generate session and run IDs
    session_id = str(uuid.uuid4())
    run_id = f"run-{uuid.uuid4().hex[:12]}"

    # 10. Build initial_variables (merge factory's with impl's own)
    effective_initial_variables: dict[str, Any] = {}
    if initial_variables:
        effective_initial_variables.update(initial_variables)
    if resolved_task_id:
        effective_initial_variables["assigned_task_id"] = (
            f"#{task_seq_num}" if task_seq_num else resolved_task_id
        )
    if enhanced_prompt:
        effective_initial_variables["prompt"] = enhanced_prompt

    # 11. Execute spawn via SpawnExecutor
    spawn_request = SpawnRequest(
        prompt=enhanced_prompt,
        cwd=isolation_ctx.cwd,
        mode=effective_mode,
        provider=effective_provider,
        session_id=session_id,
        run_id=run_id,
        agent_run_id=run_id,
        parent_session_id=parent_session_id,
        project_id=project_id,
        workflow=effective_workflow,
        initial_variables=effective_initial_variables,
        worktree_id=isolation_ctx.worktree_id,
        clone_id=isolation_ctx.clone_id,
        branch_name=isolation_ctx.branch_name,
        session_manager=runner.child_session_manager,
        machine_id=get_machine_id() or "unknown",
        model=effective_model,
        sandbox_config=effective_sandbox_config,
    )

    # 11b. Pre-register with RunningAgentRegistry before spawn
    agent_registry = get_running_agent_registry()
    agent_registry.add(
        RunningAgent(
            run_id=run_id,
            session_id=session_id,
            parent_session_id=parent_session_id,
            mode=effective_mode,
            provider=effective_provider,
            workflow_name=effective_workflow,
            worktree_id=isolation_ctx.worktree_id,
            clone_id=isolation_ctx.clone_id,
            timeout_seconds=effective_timeout,
        )
    )

    # NOTE: agent_runs DB record is created inside prepare_terminal_spawn()
    # (called by execute_spawn).  Do NOT pre-create here — it causes a
    # UNIQUE constraint violation since prepare_terminal_spawn also inserts
    # with the same run_id.  See: agents/spawn.py:162

    spawn_result = await execute_spawn(spawn_request)

    # 12. Update or remove registry entry based on spawn result
    if spawn_result.success and spawn_result.child_session_id is not None:
        agent_registry.add(
            RunningAgent(
                run_id=run_id,
                session_id=spawn_result.child_session_id,
                parent_session_id=parent_session_id,
                mode=effective_mode,
                pid=spawn_result.pid,
                terminal_type=spawn_result.terminal_type,
                tmux_session_name=spawn_result.tmux_session_name,
                provider=effective_provider,
                workflow_name=effective_workflow,
                worktree_id=isolation_ctx.worktree_id,
                clone_id=isolation_ctx.clone_id,
                timeout_seconds=effective_timeout,
            )
        )
        try:
            runner.run_storage.update_child_session(run_id, spawn_result.child_session_id)
        except Exception as e:
            logger.warning(f"Failed to update child_session_id for {run_id}: {e}")

        # Post-spawn health check: verify tmux session is still alive.
        if spawn_result.terminal_type == "tmux" and spawn_result.tmux_session_name:

            async def _deferred_health_check(
                _run_id: str,
                _tmux_name: str,
                _delay: float,
            ) -> None:
                try:
                    await asyncio.sleep(_delay)
                    tmux_cfg = TmuxConfig()
                    alive = await _check_tmux_session_alive(
                        _tmux_name, socket_name=tmux_cfg.socket_name
                    )
                    if not alive:
                        logger.error(
                            f"Agent {_run_id} tmux session '{_tmux_name}' "
                            f"exited immediately after spawn"
                        )
                        agent_registry.remove(_run_id, status="failed")
                        try:
                            runner.run_storage.fail(
                                _run_id,
                                error="Agent process exited immediately after spawn",
                            )
                        except Exception as e:
                            logger.warning(f"Failed to mark agent_run {_run_id} as failed: {e}")
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning(f"Deferred health check for {_run_id} failed: {e}")

            health_task = asyncio.create_task(
                _deferred_health_check(
                    run_id, spawn_result.tmux_session_name, TMUX_HEALTH_CHECK_DELAY
                ),
                name=f"tmux-health-{run_id}",
            )
            _health_check_tasks.add(health_task)
            health_task.add_done_callback(_health_check_tasks.discard)
    else:
        # Spawn failed — remove pre-registered entry and mark DB record as failed
        agent_registry.remove(run_id, status="failed")
        try:
            runner.run_storage.fail(run_id, error=spawn_result.error or "Spawn failed")
        except Exception as e:
            logger.warning(f"Failed to mark agent_run {run_id} as failed: {e}")

    # 13. Return response with isolation metadata
    if not spawn_result.success:
        return {"success": False, "error": spawn_result.error or "Failed to spawn agent"}

    return {
        "success": True,
        "run_id": run_id,
        "child_session_id": spawn_result.child_session_id,
        "status": spawn_result.status,
        "isolation": effective_isolation,
        "branch_name": isolation_ctx.branch_name,
        "worktree_id": isolation_ctx.worktree_id,
        "worktree_path": isolation_ctx.cwd if effective_isolation == "worktree" else None,
        "clone_id": isolation_ctx.clone_id,
        "pid": spawn_result.pid,
        "message": spawn_result.message,
    }
