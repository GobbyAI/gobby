"""Core spawn_agent implementation.

Contains spawn_agent_impl() — the internal implementation used by both
the spawn_agent MCP tool and direct callers.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from gobby.agents.isolation import (
    SpawnConfig,
    ensure_isolation_code_index,
    get_isolation_handler,
    provider_mcp_config_error,
    repair_isolation_environment,
)
from gobby.agents.reasoning import resolve_spawn_reasoning
from gobby.agents.sandbox import SandboxConfig, agent_sandbox_config
from gobby.agents.spawn_executor import SpawnRequest, execute_spawn
from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_actionable
from gobby.utils.machine_id import get_machine_id
from gobby.utils.project_context import get_project_context
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

from ._health import TMUX_HEALTH_CHECK_DELAY, _check_tmux_session_alive, _health_check_tasks

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | dict):
        return []
    return [item for item in value if isinstance(item, str)]


def _normalize_optional_model(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _defaulted_provider(value: str | None) -> str:
    if value is None or value == "inherit":
        return "claude"
    return value


def _transition_condition_met(condition: str | None, variables: dict[str, Any]) -> bool:
    if not condition:
        return True
    try:
        evaluator = SafeExpressionEvaluator(
            context={"vars": variables, "variables": variables},
            allowed_funcs={
                "len": len,
                "bool": bool,
                "str": str,
                "int": int,
                "list": list,
                "dict": dict,
                "any": any,
                "all": all,
            },
        )
        return evaluator.evaluate(condition)
    except ValueError as exc:
        logger.warning("Failed to evaluate initial step transition %r: %s", condition, exc)
        return False


def _advance_initial_step(
    agent_body: AgentDefinitionBody,
    current_step: str,
    variables: dict[str, Any],
) -> str:
    steps = {step.name: step for step in (agent_body.steps or [])}
    max_transitions = len(steps) + 1

    for _ in range(max_transitions):
        step = steps.get(current_step)
        if step is None:
            return current_step

        for transition in step.transitions:
            if not _transition_condition_met(transition.when, variables):
                continue
            if transition.to not in steps:
                logger.warning(
                    "Initial step transition to unknown step %r in agent %r",
                    transition.to,
                    agent_body.name,
                )
                continue
            current_step = transition.to
            break
        else:
            return current_step

    logger.warning(
        "Stopped initial step transition chain for agent %r after %d transitions",
        agent_body.name,
        max_transitions,
    )
    return current_step


def _initial_step_state_for_spawn(
    agent_body: AgentDefinitionBody,
    *,
    task_owned_by_child: bool,
    initial_variables: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the initial step workflow state for a spawned agent."""
    step_variables = dict(agent_body.step_variables)
    if initial_variables and "additional_skills" in initial_variables:
        step_variables["additional_skills"] = initial_variables["additional_skills"]

    additional_skills = _normalize_string_list(step_variables.get("additional_skills"))
    step_variables["additional_skills"] = additional_skills
    step_variables["additional_skills_loaded"] = not additional_skills or all(
        skill in _normalize_string_list(step_variables.get("loaded_skills"))
        for skill in additional_skills
    )

    steps = agent_body.steps or []
    if not steps:
        raise ValueError("Cannot initialize step state for an agent with no steps")
    first_step = steps[0]
    current_step = first_step.name

    if task_owned_by_child and first_step.name == "claim":
        step_variables["task_claimed"] = True

    current_step = _advance_initial_step(agent_body, current_step, step_variables)

    return current_step, step_variables


def _persist_spawn_runtime(
    runner: Any,
    run_id: str,
    spawn_result: Any,
    *,
    tmux_session_name: str | None,
    worktree_id: str | None,
    clone_id: str | None,
) -> None:
    child_session_id = getattr(spawn_result, "child_session_id", None)
    if child_session_id is not None:
        try:
            runner.run_storage.update_child_session(run_id, child_session_id)
        except Exception as e:
            logger.warning(f"Failed to update child_session_id for {run_id}: {e}")

    try:
        runner.run_storage.update_runtime(
            run_id,
            pid=getattr(spawn_result, "pid", None),
            tmux_session_name=tmux_session_name,
            worktree_id=worktree_id,
            clone_id=clone_id,
        )
    except Exception as e:
        logger.warning(f"Failed to persist runtime state for {run_id}: {e}")


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
    initial_variables: dict[str, Any] | None = None,
    session_manager: Any | None = None,  # SessionManager
    db: Any | None = None,  # DatabaseProtocol
    daemon_config: Any | None = None,  # DaemonConfig
    code_index: Any | None = None,  # CodeIndexContext
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
        mode: Execution mode (interactive/autonomous)
        provider: AI provider (claude/gemini/qwen/codex/droid)
        model: Model to use
        timeout: Timeout in seconds
        max_turns: Maximum conversation turns
        parent_session_id: Parent session ID
        project_path: Project path override
        initial_variables: Pre-built initial variables from factory (merged with impl's own)
        session_manager: SessionManager for mode=self
        db: DatabaseProtocol for mode=self

    Returns:
        Dict with success status, run_id, child_session_id, isolation metadata
    """
    # 0. Plan-validation gate for planning agents.
    # planner / plan-adversary spawns refuse to start when the task's
    # plan artifact fails the Plan-Coverage Contract validator. Catches
    # structural drift the parser silently drops before wasting an LLM call.
    from gobby.tasks.expansion._plan_gate import validate_plan_for_agent_spawn

    gate_failure = validate_plan_for_agent_spawn(
        agent_lookup_name,
        task_id,
        task_manager,
        code_index=code_index,
    )
    if gate_failure is not None:
        return gate_failure

    # 1. Merge config: agent_body defaults < params
    _raw_isolation: str | None = isolation
    if _raw_isolation is None and agent_body:
        _raw_isolation = agent_body.isolation
    if _raw_isolation in (None, "inherit"):
        _raw_isolation = "none"
    effective_isolation = cast(
        Literal["none", "worktree", "clone"],
        _raw_isolation if _raw_isolation in ("none", "worktree", "clone") else "none",
    )

    provider_was_overridden = provider is not None
    _raw_provider: str | None = provider
    if _raw_provider is None and agent_body:
        _raw_provider = agent_body.provider
    effective_provider = _defaulted_provider(_raw_provider)

    provider_differs_from_agent = False
    if provider_was_overridden and agent_body:
        provider_differs_from_agent = effective_provider != _defaulted_provider(agent_body.provider)

    effective_model = _normalize_optional_model(model)
    if effective_model is None and agent_body and not provider_differs_from_agent:
        effective_model = _normalize_optional_model(agent_body.model)
    from gobby.llm.local_detection import is_local_agent_definition

    is_local_run = is_local_agent_definition(effective_provider, effective_model)

    requested_reasoning_effort = reasoning_effort
    if requested_reasoning_effort is None and agent_body:
        requested_reasoning_effort = agent_body.reasoning_effort
    effective_reasoning_required = reasoning_required
    if effective_reasoning_required is None and agent_body:
        effective_reasoning_required = agent_body.reasoning_required

    reasoning = resolve_spawn_reasoning(
        provider=effective_provider,
        model=effective_model,
        requested_effort=requested_reasoning_effort,
        reasoning_required=effective_reasoning_required,
        daemon_config=daemon_config,
    )
    if reasoning.reasoning_required and reasoning.effective_effort is None:
        return {
            "success": False,
            "error": reasoning.message or "Requested reasoning is not supported",
            "reasoning": reasoning.to_dict(),
        }

    # Resolve api_base/api_token from agent definition (with ${ENV_VAR} expansion)
    effective_api_base: str | None = None
    effective_api_token: str | None = None
    if agent_body:
        effective_api_base = agent_body.api_base
        if agent_body.api_token:
            token = agent_body.api_token
            if token.startswith("${") and token.endswith("}"):
                import os

                effective_api_token = os.environ.get(token[2:-1])
            else:
                effective_api_token = token

    # Resolve model: local from daemon config
    if effective_model == "local":
        from gobby.config.app import LocalConfig

        local_cfg: LocalConfig | None = (
            getattr(daemon_config, "local", None) if daemon_config else None
        )
        if not local_cfg:
            return {
                "success": False,
                "error": "model: local requires a 'local' section in daemon config "
                "(local.url, local.model)",
            }
        effective_api_base = effective_api_base or local_cfg.url
        effective_model = local_cfg.model
        if not effective_api_token and local_cfg.api_key:
            effective_api_token = local_cfg.api_key

        # Pre-flight: ensure correct model is loaded (returns resolved name for auto mode)
        try:
            from gobby.agents.local_model import ensure_local_model

            registry = runner.registry if hasattr(runner, "registry") else None
            effective_model = await ensure_local_model(local_cfg, registry=registry)
        except Exception as e:
            return {"success": False, "error": f"Local model pre-flight failed: {e}"}

    effective_timeout = timeout
    if effective_timeout is None and agent_body and agent_body.timeout:
        effective_timeout = agent_body.timeout
    if effective_timeout == 0:
        effective_timeout = None  # 0 means no timeout

    effective_workflow = workflow

    effective_base_branch = base_branch
    if effective_base_branch is None and agent_body:
        effective_base_branch = agent_body.base_branch
    # "inherit" means "resolve from context", treat as unset
    if effective_base_branch == "inherit":
        effective_base_branch = None
    # Auto-detect current branch if no base_branch specified
    if effective_base_branch is None and git_manager:
        try:
            effective_base_branch = git_manager.get_current_branch()
        except Exception as e:
            logger.debug(f"Failed to auto-detect current branch: {e}", exc_info=True)
            effective_base_branch = None
    effective_base_branch = effective_base_branch or "main"

    # Daemon-owned agent sandboxes inherit from config-store defaults only.
    effective_sandbox_config: SandboxConfig = agent_sandbox_config(daemon_config)

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
    task_category: str | None = None
    task_additional_skills: list[str] | None = None
    claimed_session_id: str | None = None
    task_owned_by_child = False

    if task_id and task_manager:
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id, project_id)
            task = task_manager.get_task(resolved_task_id)
            if task:
                task_title = task.title
                task_seq_num = task.seq_num
                task_category = getattr(task, "category", None)
                if task.additional_skills is not None:
                    task_additional_skills = _normalize_string_list(task.additional_skills)
                claimed_session_id = get_claimed_session_id(task)
        except Exception as e:
            logger.warning(f"Failed to resolve task_id {task_id}: {e}")

    # 4b. Dedup check — idempotent: return success if agent already running
    if resolved_task_id and runner.run_storage:
        if runner.run_storage.has_active_run_for_task(resolved_task_id):
            active_run = runner.run_storage.get_active_run_for_task(resolved_task_id)
            return {
                "success": True,
                "skipped": True,
                "run_id": active_run.id if active_run else None,
                "message": f"Agent already running for task {task_id}",
            }

    # 5. Handle worktree_id/clone_id reuse: skip isolation creation when existing resource provided
    isolation_ctx = None
    if worktree_id and worktree_storage:
        existing_worktree = worktree_storage.get(worktree_id)
        if not existing_worktree:
            return {"success": False, "error": f"Worktree {worktree_id} not found"}

        # Verify worktree directory still exists on disk
        if not Path(existing_worktree.worktree_path).is_dir():
            worktree_storage.delete(worktree_id)
            return {
                "success": False,
                "error": f"Worktree directory missing: {existing_worktree.worktree_path} (stale record cleaned up)",
            }

        try:
            await repair_isolation_environment(
                main_repo_path=resolved_project_path,
                isolated_path=existing_worktree.worktree_path,
                provider=effective_provider,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to repair worktree isolation: {e}"}

        from gobby.agents.isolation import IsolationContext

        isolation_ctx = IsolationContext(
            cwd=existing_worktree.worktree_path,
            branch_name=existing_worktree.branch_name,
            worktree_id=existing_worktree.id,
            isolation_type="worktree",
            extra={"main_repo_path": resolved_project_path, "reused_worktree": True},
        )
        effective_isolation = "worktree"
        handler = get_isolation_handler("none")
    elif clone_id and clone_storage:
        existing_clone = clone_storage.get(clone_id)
        if not existing_clone:
            return {"success": False, "error": f"Clone {clone_id} not found"}

        # Verify clone directory still exists on disk
        if not Path(existing_clone.clone_path).is_dir():
            clone_storage.delete(clone_id)
            return {
                "success": False,
                "error": f"Clone directory missing: {existing_clone.clone_path} (stale record cleaned up)",
            }

        try:
            await repair_isolation_environment(
                main_repo_path=resolved_project_path,
                isolated_path=existing_clone.clone_path,
                provider=effective_provider,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to repair clone isolation: {e}"}

        from gobby.agents.isolation import IsolationContext

        isolation_ctx = IsolationContext(
            cwd=existing_clone.clone_path,
            branch_name=existing_clone.branch_name,
            clone_id=existing_clone.id,
            isolation_type="clone",
            extra={"source_repo": resolved_project_path, "reused_clone": True},
        )
        effective_isolation = "clone"
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

    code_index_preflight_warning: dict[str, str] | None = None
    if effective_isolation in {"worktree", "clone"}:
        config_error = provider_mcp_config_error(isolation_ctx.cwd, effective_provider)
        if config_error is not None:
            return {"success": False, "error": config_error}
        if task_category != "docs":
            try:
                await ensure_isolation_code_index(isolation_ctx.cwd)
            except Exception as e:
                reason = str(e)
                logger.warning(
                    "Continuing isolated spawn after code index preflight failed "
                    "for cwd=%s: %s",
                    isolation_ctx.cwd,
                    reason,
                )
                code_index_preflight_warning = {
                    "preflight": "code_index",
                    "cwd": isolation_ctx.cwd,
                    "message": reason,
                }

    # 8. Build enhanced prompt with isolation context
    enhanced_prompt = handler.build_context_prompt(prompt, isolation_ctx)

    # 9. Generate session and run IDs
    session_id = str(uuid.uuid4())
    run_id = f"run-{uuid.uuid4().hex[:12]}"

    # 10. Build initial_variables (merge factory's with impl's own)
    effective_initial_variables: dict[str, Any] = {}
    if initial_variables:
        effective_initial_variables.update(initial_variables)
    if reasoning.status != "not_requested":
        effective_initial_variables.update(
            {
                "_requested_reasoning_effort": reasoning.requested_effort,
                "_effective_reasoning_effort": reasoning.effective_effort,
                "_reasoning_required": reasoning.reasoning_required,
                "_reasoning_status": reasoning.status,
            }
        )
    if resolved_task_id:
        effective_initial_variables["assigned_task_id"] = (
            f"#{task_seq_num}" if task_seq_num else resolved_task_id
        )
    if enhanced_prompt:
        effective_initial_variables["prompt"] = enhanced_prompt
    if code_index_preflight_warning is not None:
        effective_initial_variables["code_index_preflight_warning"] = (
            code_index_preflight_warning
        )
    additional_skills = _normalize_string_list(effective_initial_variables.get("additional_skills"))
    if task_additional_skills is not None:
        additional_skills = task_additional_skills
    effective_initial_variables["additional_skills"] = additional_skills

    # 10b. Inject isolation context so workflow variables can reference them
    if isolation_ctx.clone_id:
        effective_initial_variables["clone_id"] = isolation_ctx.clone_id
    if isolation_ctx.worktree_id:
        effective_initial_variables["worktree_id"] = isolation_ctx.worktree_id
    if isolation_ctx.branch_name:
        effective_initial_variables["branch_name"] = isolation_ctx.branch_name

    # 11. Build a meaningful session title from agent name and/or task
    agent_display_name = agent_lookup_name or (agent_body.name if agent_body else None)
    if agent_display_name and task_title:
        spawn_title = f"{agent_display_name}: {task_title}"
    elif agent_display_name:
        spawn_title = agent_display_name
    elif task_title:
        task_ref = f"#{task_seq_num}" if task_seq_num else ""
        spawn_title = f"Agent: {task_ref} {task_title}".strip()
    else:
        spawn_title = None  # fall back to existing default in create_child_session

    # 11a. Execute spawn via SpawnExecutor
    spawn_request = SpawnRequest(
        prompt=enhanced_prompt,
        cwd=isolation_ctx.cwd,
        provider=effective_provider,
        session_id=session_id,
        run_id=run_id,
        agent_run_id=run_id,
        parent_session_id=parent_session_id,
        project_id=project_id,
        project_path=resolved_project_path,
        workflow=effective_workflow,
        initial_variables=effective_initial_variables,
        worktree_id=isolation_ctx.worktree_id,
        clone_id=isolation_ctx.clone_id,
        branch_name=isolation_ctx.branch_name,
        task_id=resolved_task_id,
        claimed_session_id=claimed_session_id,
        title=spawn_title,
        agent_name=agent_display_name,
        session_manager=runner.child_session_manager,
        machine_id=get_machine_id() or "unknown",
        model=effective_model,
        is_local=is_local_run,
        api_base=effective_api_base,
        api_token=effective_api_token,
        requested_reasoning_effort=reasoning.requested_effort,
        effective_reasoning_effort=reasoning.effective_effort,
        reasoning_required=reasoning.reasoning_required,
        reasoning_status=reasoning.status,
        reasoning_message=reasoning.message,
        sandbox_config=effective_sandbox_config,
        timeout_seconds=effective_timeout,
        daemon_config=daemon_config,
    )

    # run_id is minted above and threaded through SpawnRequest.agent_run_id.
    # prepare_terminal_spawn (called inside execute_spawn) inserts the
    # agent_runs row using that exact id. It is the single source of truth.

    spawn_result = await execute_spawn(spawn_request)
    tmux_session_name = getattr(spawn_result, "tmux_session_name", None)
    if not isinstance(tmux_session_name, str):
        tmux_session_name = None
    tmux_socket_name = getattr(spawn_result, "tmux_socket_name", None)
    if not isinstance(tmux_socket_name, str):
        tmux_socket_name = None
    tmux_socket_path = getattr(spawn_result, "tmux_socket_path", None)
    if not isinstance(tmux_socket_path, str):
        tmux_socket_path = None

    tmux_spawn = bool(
        spawn_result.success and spawn_result.terminal_type == "tmux" and tmux_session_name
    )
    runtime_persisted = False
    if tmux_spawn and tmux_session_name:
        alive = await _check_tmux_session_alive(
            tmux_session_name,
            socket_name=tmux_socket_name,
            socket_path=tmux_socket_path,
        )
        if not alive:
            spawn_result.success = False
            spawn_result.status = "failed"
            spawn_result.error = f"tmux session '{tmux_session_name}' failed live-pane verification"
            tmux_spawn = False

    if tmux_spawn and tmux_session_name:
        if spawn_result.child_session_id is not None:
            _persist_spawn_runtime(
                runner,
                run_id,
                spawn_result,
                tmux_session_name=tmux_session_name,
                worktree_id=isolation_ctx.worktree_id,
                clone_id=isolation_ctx.clone_id,
            )
            runtime_persisted = True
            try:
                runner.run_storage.start(run_id)
            except Exception as e:
                logger.warning(f"Failed to mark agent run {run_id} as running: {e}")

    # 12. Update DB and handle post-spawn setup based on spawn result
    if spawn_result.success and spawn_result.child_session_id is not None:
        if not runtime_persisted:
            _persist_spawn_runtime(
                runner,
                run_id,
                spawn_result,
                tmux_session_name=tmux_session_name,
                worktree_id=isolation_ctx.worktree_id,
                clone_id=isolation_ctx.clone_id,
            )

        if not tmux_spawn:
            try:
                runner.run_storage.start(run_id)
            except Exception as e:
                logger.warning(f"Failed to mark agent run {run_id} as running: {e}")

        # Fire agent_started event for WebSocket broadcasting
        try:
            from gobby.runner_broadcasting import fire_agent_event

            fire_agent_event(
                "agent_started",
                run_id,
                {
                    "session_id": spawn_result.child_session_id,
                    "parent_session_id": parent_session_id,
                    "provider": effective_provider,
                    "pid": spawn_result.pid,
                    "tmux_session_name": tmux_session_name,
                    "tmux_socket_name": tmux_socket_name,
                    "tmux_socket_path": tmux_socket_path,
                },
            )
        except Exception as e:
            logger.debug(f"Failed to fire agent_started event for {run_id}: {e}")

        # 12a. Auto-claim task if task_id was provided.
        if resolved_task_id and task_manager:
            try:
                task_obj = task_manager.get_task(resolved_task_id)
                if not task_obj or not is_task_actionable(task_obj):
                    logger.info(
                        "Skipping auto-claim for task %s; task is not actionable",
                        f"#{task_seq_num}" if task_seq_num else resolved_task_id,
                    )
                elif (
                    current_owner := get_claimed_session_id(task_obj)
                ) and current_owner != spawn_result.child_session_id:
                    logger.info(
                        "Skipping auto-claim for task %s; already assigned to %s",
                        f"#{task_seq_num}" if task_seq_num else resolved_task_id,
                        current_owner,
                    )
                else:
                    claimed_task = task_manager.claim_task(
                        resolved_task_id,
                        session_id=spawn_result.child_session_id,
                    )
                    task_owned_by_child = (
                        get_claimed_session_id(claimed_task) == spawn_result.child_session_id
                    )
                    logger.info(
                        f"Auto-claimed task {(f'#{task_seq_num}' if task_seq_num else resolved_task_id)} for agent {run_id} (session {spawn_result.child_session_id})",
                    )
            except Exception as e:
                logger.warning(f"Failed to auto-claim task {resolved_task_id}: {e}")

        # 12b. Create WorkflowInstance for agent step workflow (post-spawn).
        # Must happen AFTER execute_spawn creates the child session record,
        # because workflow_instances.session_id has a FK to sessions(id).
        # Uses spawn_result.child_session_id (the real session) instead of
        # the pre-generated session_id which is not the actual child session
        # for terminal mode.
        step_wf_name = (initial_variables or {}).get("_step_workflow_name")
        if step_wf_name and agent_body and agent_body.steps and db:
            try:
                from gobby.workflows.definitions import WorkflowInstance
                from gobby.workflows.state_manager import WorkflowInstanceManager

                current_step, step_variables = _initial_step_state_for_spawn(
                    agent_body,
                    task_owned_by_child=task_owned_by_child,
                    initial_variables=effective_initial_variables,
                )
                step_instance = WorkflowInstance(
                    id=str(uuid.uuid4()),
                    session_id=spawn_result.child_session_id,
                    workflow_name=step_wf_name,
                    enabled=True,
                    priority=10,
                    current_step=current_step,
                    variables=step_variables,
                )
                WorkflowInstanceManager(db).save_instance(step_instance)

                # Initialize step_workflow_complete so the require-step-completion
                # rule can gate agent stop until the exit_condition is met.
                from gobby.workflows.state_manager import SessionVariableManager

                SessionVariableManager(db).set_variable(
                    spawn_result.child_session_id, "step_workflow_complete", False
                )

                logger.info(
                    f"Created step workflow instance {step_wf_name} for session {spawn_result.child_session_id} (agent={agent_body.name}, step={agent_body.steps[0].name})",
                )
            except Exception as e:
                logger.error(f"Failed to create step workflow instance: {e}", exc_info=True)

        # Post-spawn health check: verify tmux session is still alive.
        if spawn_result.terminal_type == "tmux" and tmux_session_name:

            async def _deferred_health_check(
                _run_id: str,
                _tmux_name: str,
                _socket_name: str | None,
                _socket_path: str | None,
                _delay: float,
            ) -> None:
                try:
                    await asyncio.sleep(_delay)
                    alive = await _check_tmux_session_alive(
                        _tmux_name,
                        socket_name=_socket_name,
                        socket_path=_socket_path,
                    )
                    if not alive:
                        logger.error(
                            f"Agent {_run_id} tmux session '{_tmux_name}' "
                            f"exited immediately after spawn"
                        )
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
                    run_id,
                    tmux_session_name,
                    tmux_socket_name,
                    tmux_socket_path,
                    TMUX_HEALTH_CHECK_DELAY,
                ),
                name=f"tmux-health-{run_id}",
            )
            _health_check_tasks.add(health_task)
            health_task.add_done_callback(_health_check_tasks.discard)
    else:
        # Spawn failed — mark DB record as failed
        try:
            runner.run_storage.fail(run_id, error=spawn_result.error or "Spawn failed")
        except Exception as e:
            logger.warning(f"Failed to mark agent_run {run_id} as failed: {e}")

    # 13. Return response with isolation metadata
    if not spawn_result.success:
        return {
            "success": False,
            "error": spawn_result.error or "Failed to spawn agent",
            "reasoning": reasoning.to_dict(),
        }

    response = {
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
        "tmux_session_name": tmux_session_name,
        "tmux_socket_name": tmux_socket_name,
        "tmux_socket_path": tmux_socket_path,
        "message": spawn_result.message,
        "reasoning": reasoning.to_dict(),
    }
    if code_index_preflight_warning is not None:
        response["warnings"] = [code_index_preflight_warning]
    return response
