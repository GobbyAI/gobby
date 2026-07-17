"""Core spawn_agent implementation.

Contains spawn_agent_impl() — the internal implementation used by both
the spawn_agent MCP tool and direct callers.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from gobby.agents.isolation import (
    CloneIsolationHandler,
    IsolationHandler,
    SpawnConfig,
    WorktreeIsolationHandler,
    get_isolation_handler,
    provider_mcp_config_error,
    repair_isolation_environment,
)
from gobby.agents.reasoning import resolve_spawn_reasoning
from gobby.agents.resume_metadata import build_resume_metadata
from gobby.agents.sandbox import SandboxConfig, agent_sandbox_config
from gobby.agents.spawn_executor import execute_spawn
from gobby.agents.spawn_models import SpawnRequest
from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_actionable
from gobby.utils.machine_id import get_machine_id
from gobby.utils.project_context import get_project_context
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

from ._code_index import (
    append_code_index_warning,
    prepare_spawn_code_index,
    without_code_index_skill,
)
from ._failure_cleanup import cleanup_created_isolation, cleanup_failed_spawn, start_run_or_cleanup
from ._health import _check_tmux_session_alive, schedule_tmux_health_check
from ._idempotency import non_actionable_task_spawn_response
from ._provider_resolution import defaulted_provider, provider_prefixed_model
from ._runtime import (
    _build_spawn_success_response,
    _normalize_optional_model,
    _normalize_string_list,
    _persist_spawn_runtime,
    _tmux_runtime_metadata,
)
from ._spawn_guards import (
    TaskSpawnLease,
    active_task_response_if_blocked,
    reserve_agent_slot,
)
from ._worktree_reuse import prepare_reused_worktree

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


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
    # Context
    parent_session_id: str | None = None,
    project_path: str | None = None,
    initial_variables: dict[str, Any] | None = None,
    session_manager: Any | None = None,  # SessionManager
    db: Any | None = None,  # HubDatabase
    daemon_config: Any | None = None,  # DaemonConfig
    code_index: Any | None = None,  # CodeIndexContext
    held_task_mutex: Any | None = None,
) -> dict[str, Any]:
    """Core spawn_agent implementation used by the MCP tool and direct callers."""
    # 0. Plan-validation gate for planning agents.
    # planner / plan-adversary spawns refuse to start when the task's
    # plan artifact fails the Plan-Coverage Contract validator. Catches
    # structural drift the parser silently drops before wasting an LLM call.
    from gobby.tasks.expansion._plan_gate import validate_plan_for_agent_spawn

    gate_failure = await asyncio.to_thread(
        validate_plan_for_agent_spawn,
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
    model_from_prefix = provider_prefixed_model(_normalize_optional_model(model))
    _raw_provider: str | None = provider
    if _raw_provider is None and model_from_prefix is not None:
        _raw_provider = model_from_prefix[0]
    if _raw_provider is None and agent_body:
        _raw_provider = agent_body.provider
    effective_provider = defaulted_provider(_raw_provider)

    if provider_was_overridden and model_from_prefix and model_from_prefix[0] != effective_provider:
        return {
            "success": False,
            "error": (
                "model provider prefix "
                f"'{model_from_prefix[0]}' does not match explicit provider "
                f"'{effective_provider}'"
            ),
        }

    provider_differs_from_agent = False
    if provider_was_overridden and agent_body:
        provider_differs_from_agent = effective_provider != defaulted_provider(agent_body.provider)

    effective_model = (
        model_from_prefix[1] if model_from_prefix else _normalize_optional_model(model)
    )
    if effective_model is None and agent_body and not provider_differs_from_agent:
        effective_model = _normalize_optional_model(agent_body.model)
    is_local_run = False

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

    from gobby.mcp_proxy.tools.spawn_agent._local_endpoint import resolve_spawn_local_endpoint

    try:
        local_resolution = await resolve_spawn_local_endpoint(
            model=effective_model,
            api_base=effective_api_base,
            api_token=effective_api_token,
            daemon_config=daemon_config,
            run_manager=runner.run_storage,
            runtime_provider=effective_provider,
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    effective_model = local_resolution.model
    effective_api_base = local_resolution.api_base
    effective_api_token = local_resolution.api_token
    is_local_run = local_resolution.is_local

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
            logger.debug("Failed to auto-detect current branch: %s", e, exc_info=True)
            effective_base_branch = None
    effective_base_branch = effective_base_branch or "main"

    # Daemon-owned agent sandboxes inherit from config-store defaults only.
    effective_sandbox_config: SandboxConfig = agent_sandbox_config(daemon_config)
    requested_agent_name = agent_lookup_name or (agent_body.name if agent_body else None)

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
    resolved_task: Any | None = None

    if task_id and task_manager:
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id, project_id)
            resolved_task = task_manager.get_task(resolved_task_id)
            if resolved_task:
                task_title = resolved_task.title
                task_seq_num = resolved_task.seq_num
                task_category = getattr(resolved_task, "category", None)
                if resolved_task.additional_skills is not None:
                    task_additional_skills = _normalize_string_list(resolved_task.additional_skills)
                claimed_session_id = get_claimed_session_id(resolved_task)
        except Exception as e:
            logger.warning("Failed to resolve task_id %s: %s", task_id, e)

    if resolved_task_id and resolved_task is not None and not is_task_actionable(resolved_task):
        return non_actionable_task_spawn_response(
            resolved_task, task_ref=task_id, resolved_task_id=resolved_task_id
        )
    # 5. Build spawn config and handle worktree_id/clone_id reuse.
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

    # Explicit reuse skips isolation creation when the existing resource can be prepared.
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

        if git_manager is None:
            return {"success": False, "error": "git_manager is required to reuse a worktree"}

        try:
            isolation_ctx, handler = await prepare_reused_worktree(
                existing_worktree=existing_worktree,
                git_manager=git_manager,
                worktree_storage=worktree_storage,
                clone_manager=clone_manager,
                clone_storage=clone_storage,
                spawn_config=spawn_config,
                main_repo_path=resolved_project_path,
            )
            effective_isolation = "worktree"
            context_handler: IsolationHandler = WorktreeIsolationHandler(
                git_manager, worktree_storage
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to prepare reused worktree: {e}"}
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
        context_handler = CloneIsolationHandler(clone_manager, clone_storage, git_manager)
    else:
        # Normal isolation flow
        handler = get_isolation_handler(
            effective_isolation,
            git_manager=git_manager,
            worktree_storage=worktree_storage,
            clone_manager=clone_manager,
            clone_storage=clone_storage,
        )
        context_handler = handler

    cleanup_isolation_on_failure = not (worktree_id or clone_id)
    if isolation_ctx is None:
        try:
            isolation_ctx = await handler.prepare_environment(spawn_config)
        except Exception as e:
            logger.error("Failed to prepare environment: %s", e, exc_info=True)
            try:
                await handler.cleanup_environment(spawn_config)
            except Exception as cleanup_err:
                logger.warning("Cleanup after prepare failure also failed: %s", cleanup_err)
            return {"success": False, "error": f"Failed to prepare environment: {e}"}

    if effective_isolation in {"worktree", "clone"}:
        config_error = provider_mcp_config_error(isolation_ctx.cwd, effective_provider)
        if config_error is not None:
            await cleanup_created_isolation(
                handler, spawn_config, cleanup=cleanup_isolation_on_failure
            )
            return {"success": False, "error": config_error}
    code_index_preflight = await prepare_spawn_code_index(
        cwd=isolation_ctx.cwd,
        daemon_config=daemon_config,
        isolation=effective_isolation,
        agent_name=requested_agent_name,
        initial_variables=initial_variables,
        task_category=task_category,
    )
    if code_index_preflight.error is not None:
        await cleanup_created_isolation(handler, spawn_config, cleanup=cleanup_isolation_on_failure)
        return {"success": False, "error": code_index_preflight.error}
    code_index_preflight_warning = code_index_preflight.warning
    code_index_preflight_env = code_index_preflight.env or {}

    # 8. Build enhanced prompt with isolation context
    enhanced_prompt = context_handler.build_context_prompt(prompt, isolation_ctx)
    if code_index_preflight_warning is not None:
        enhanced_prompt = append_code_index_warning(enhanced_prompt, code_index_preflight_warning)

    # 9. Generate session and run IDs
    session_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

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
        effective_initial_variables["code_index_preflight_warning"] = code_index_preflight_warning
    additional_skills = _normalize_string_list(effective_initial_variables.get("additional_skills"))
    if task_additional_skills is not None:
        additional_skills = task_additional_skills
    if code_index_preflight_warning is not None:
        additional_skills = without_code_index_skill(additional_skills)
    effective_initial_variables["additional_skills"] = additional_skills

    # 10b. Inject isolation context so workflow variables can reference them
    if isolation_ctx.clone_id:
        effective_initial_variables["clone_id"] = isolation_ctx.clone_id
    if isolation_ctx.worktree_id:
        effective_initial_variables["worktree_id"] = isolation_ctx.worktree_id
    if isolation_ctx.extra.get("reused_worktree") is True:
        effective_initial_variables["reused_worktree"] = True
    if isolation_ctx.branch_name:
        effective_initial_variables["branch_name"] = isolation_ctx.branch_name
    base_commit_sha = isolation_ctx.extra.get("base_commit_sha")
    if isinstance(base_commit_sha, str) and base_commit_sha:
        effective_initial_variables["base_commit_sha"] = base_commit_sha

    # 11. Build a meaningful session title from agent name and/or task
    agent_display_name = requested_agent_name
    if agent_display_name and task_title:
        spawn_title = f"{agent_display_name}: {task_title}"
    elif agent_display_name:
        spawn_title = agent_display_name
    elif task_title:
        task_ref = f"#{task_seq_num}" if task_seq_num else ""
        spawn_title = f"Agent: {task_ref} {task_title}".strip()
    else:
        spawn_title = None  # fall back to existing default in create_child_session

    stage_name = effective_initial_variables.get("stage_name")
    stage_state = effective_initial_variables.get("stage_state")
    resume_metadata = build_resume_metadata(
        provider=effective_provider,
        model=effective_model,
        requested_reasoning_effort=reasoning.requested_effort,
        effective_reasoning_effort=reasoning.effective_effort,
        reasoning_required=reasoning.reasoning_required,
        reasoning_status=reasoning.status,
        reasoning_message=reasoning.message,
        sandbox_config=effective_sandbox_config,
        cwd=str(isolation_ctx.cwd),
        project_id=project_id,
        project_path=resolved_project_path,
        parent_session_id=parent_session_id,
        isolation=effective_isolation,
        worktree_id=isolation_ctx.worktree_id,
        clone_id=isolation_ctx.clone_id,
        branch_name=isolation_ctx.branch_name,
        base_branch=effective_base_branch,
        base_commit_sha=base_commit_sha if isinstance(base_commit_sha, str) else None,
        task_id=resolved_task_id,
        task_ref=f"#{task_seq_num}" if task_seq_num else resolved_task_id,
        stage_name=stage_name if isinstance(stage_name, str) else None,
        stage_state=stage_state if isinstance(stage_state, str) else None,
        agent_slug=agent_display_name,
        workflow=effective_workflow,
        initial_variables=effective_initial_variables,
    )

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
        codex_oss_provider=local_resolution.codex_oss_provider,
        api_base=effective_api_base,
        api_token=effective_api_token,
        requested_reasoning_effort=reasoning.requested_effort,
        effective_reasoning_effort=reasoning.effective_effort,
        reasoning_required=reasoning.reasoning_required,
        reasoning_status=reasoning.status,
        reasoning_message=reasoning.message,
        sandbox_config=effective_sandbox_config,
        extra_env=code_index_preflight_env or None,
        timeout_seconds=effective_timeout,
        daemon_config=daemon_config,
        resume_metadata_json=resume_metadata,
    )

    # run_id is minted above and threaded through SpawnRequest.agent_run_id.
    # prepare_terminal_spawn (called inside execute_spawn) inserts the
    # agent_runs row using that exact id. It is the single source of truth.

    task_spawn_lease = TaskSpawnLease(
        db=db,
        task_id=resolved_task_id,
        held_mutex=held_task_mutex,
    )
    if resolved_task_id and runner.run_storage:
        active_response = active_task_response_if_blocked(
            run_storage=runner.run_storage,
            task_id=resolved_task_id,
            task_ref=task_id,
            requested_agent_name=requested_agent_name,
            parent_session_id=parent_session_id,
        )
        if active_response is not None:
            await cleanup_created_isolation(
                handler, spawn_config, cleanup=cleanup_isolation_on_failure
            )
            return active_response
    lease_response = task_spawn_lease.acquire()
    if lease_response is not None:
        await cleanup_created_isolation(handler, spawn_config, cleanup=cleanup_isolation_on_failure)
        return lease_response
    if resolved_task_id and runner.run_storage:
        active_response = active_task_response_if_blocked(
            run_storage=runner.run_storage,
            task_id=resolved_task_id,
            task_ref=task_id,
            requested_agent_name=requested_agent_name,
            parent_session_id=parent_session_id,
        )
        if active_response is not None:
            task_spawn_lease.release_unattached()
            await cleanup_created_isolation(
                handler, spawn_config, cleanup=cleanup_isolation_on_failure
            )
            return active_response

    async with reserve_agent_slot(
        db=db,
        project_id=project_id,
        project_path=resolved_project_path,
    ) as slot_response:
        if slot_response is not None:
            task_spawn_lease.release_unattached()
            await cleanup_created_isolation(
                handler, spawn_config, cleanup=cleanup_isolation_on_failure
            )
            return slot_response
        try:
            spawn_result = await execute_spawn(spawn_request)
        except Exception as exc:
            task_spawn_lease.release_unattached()
            await cleanup_failed_spawn(
                runner,
                run_id,
                str(exc),
                handler,
                spawn_config,
                cleanup_isolation=cleanup_isolation_on_failure,
            )
            return {"success": False, "error": str(exc), "reasoning": reasoning.to_dict()}
        if spawn_result.success:
            attach_error = task_spawn_lease.attach(run_id)
            if attach_error is not None:
                task_spawn_lease.release_unattached()
                error = f"task spawn mutex attach failed: {attach_error}"
                await cleanup_failed_spawn(
                    runner,
                    run_id,
                    error,
                    handler,
                    spawn_config,
                    cleanup_isolation=cleanup_isolation_on_failure,
                    child_session_id=spawn_result.child_session_id,
                )
                return {
                    "success": False,
                    "error": error,
                    "run_id": run_id,
                }
    tmux_session_name, tmux_socket_name, tmux_socket_path = _tmux_runtime_metadata(spawn_result)

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
            start_error = await start_run_or_cleanup(
                runner,
                run_id,
                handler,
                spawn_config,
                cleanup_isolation=cleanup_isolation_on_failure,
                child_session_id=spawn_result.child_session_id,
            )
            if start_error is not None:
                return start_error

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
            start_error = await start_run_or_cleanup(
                runner,
                run_id,
                handler,
                spawn_config,
                cleanup_isolation=cleanup_isolation_on_failure,
                child_session_id=spawn_result.child_session_id,
            )
            if start_error is not None:
                return start_error

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
            logger.debug("Failed to fire agent_started event for %s: %s", run_id, e)

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
                        "Auto-claimed task %s for agent %s (session %s)",
                        (f"#{task_seq_num}" if task_seq_num else resolved_task_id),
                        run_id,
                        spawn_result.child_session_id,
                    )
            except Exception as e:
                logger.warning("Failed to auto-claim task %s: %s", resolved_task_id, e)

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
                    "Created step workflow instance %s for session %s (agent=%s, step=%s)",
                    step_wf_name,
                    spawn_result.child_session_id,
                    agent_body.name,
                    agent_body.steps[0].name,
                )
            except Exception as e:
                logger.error("Failed to create step workflow instance: %s", e, exc_info=True)

        # Post-spawn health check: verify tmux session is still alive.
        if spawn_result.terminal_type == "tmux" and tmux_session_name:
            schedule_tmux_health_check(
                runner,
                run_id,
                tmux_session_name,
                tmux_socket_name,
                tmux_socket_path,
            )
    else:
        task_spawn_lease.release_unattached()
        await cleanup_failed_spawn(
            runner,
            run_id,
            spawn_result.error or "Spawn failed",
            handler,
            spawn_config,
            cleanup_isolation=cleanup_isolation_on_failure,
            child_session_id=spawn_result.child_session_id,
        )

    # 13. Return response with isolation metadata
    if not spawn_result.success:
        return {
            "success": False,
            "error": spawn_result.error or "Failed to spawn agent",
            "reasoning": reasoning.to_dict(),
        }

    return _build_spawn_success_response(
        run_id=run_id,
        spawn_result=spawn_result,
        effective_isolation=effective_isolation,
        isolation_ctx=isolation_ctx,
        base_commit_sha=base_commit_sha,
        tmux_session_name=tmux_session_name,
        tmux_socket_name=tmux_socket_name,
        tmux_socket_path=tmux_socket_path,
        code_index_preflight_warning=code_index_preflight_warning,
        reasoning=reasoning,
    )
