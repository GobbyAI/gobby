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
from gobby.agents.reasoning import resolve_spawn_reasoning, resolve_spawn_speed
from gobby.agents.resume_metadata import build_resume_metadata
from gobby.agents.sandbox import SandboxConfig, agent_sandbox_config
from gobby.agents.spawn import cleanup_unlaunched_spawn, prepare_terminal_spawn
from gobby.agents.spawn_executor import execute_spawn
from gobby.agents.spawn_models import SpawnRequest
from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp
from gobby.providers.capabilities.apply import SpeedUnavailableError, apply_speed, speed_result
from gobby.tasks.state_semantics import (
    get_claimed_session_id,
    is_task_actionable,
    is_task_reviewable,
)
from gobby.utils.local_token import read_local_api_token
from gobby.utils.machine_id import get_machine_id
from gobby.utils.project_context import get_project_context
from gobby.workflows.definitions import AgentDefinitionBody

from ._code_index import code_index_preflight_mode
from ._failure_cleanup import (
    cleanup_created_isolation,
    cleanup_failed_spawn,
    remember_spawn_pid,
    start_run_or_cleanup,
)
from ._health import _check_tmux_session_alive, schedule_tmux_health_check
from ._idempotency import non_actionable_task_spawn_response
from ._provider_resolution import (
    concrete_provider,
    resolve_spawn_provider,
    spawning_session_provider,
)
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
from ._step_state import apply_claimed_step_update, persist_initial_step_instance_if_resolved
from ._worktree_reuse import prepare_reused_worktree

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
    allow_closed_task: bool = False,
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
    speed_mode: Literal["standard", "fast"] = "standard",
    reasoning_effort: str | None = None,
    reasoning_required: bool | None = None,
    # Limits
    timeout: float | None = None,
    # Context
    parent_session_id: str | None = None,
    caller_session_id: str | None = None,
    project_path: str | None = None,
    initial_variables: dict[str, Any] | None = None,
    session_manager: Any | None = None,  # SessionManager
    db: Any | None = None,  # HubDatabase
    completion_registry: Any | None = None,
    daemon_config: Any | None = None,  # DaemonConfig
    code_index: Any | None = None,  # CodeIndexContext
    held_task_mutex: Any | None = None,
) -> dict[str, Any]:
    """Core spawn_agent implementation used by the MCP tool and direct callers."""
    if agent_body is not None:
        try:
            agent_body.prompt_for("agent")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    # 0. Plan-validation gate for planning agents.
    # Structural failures block planning roles. Authoring roles may continue
    # past symbol-only failures with repair diagnostics appended to the prompt.
    from gobby.tasks.expansion._plan_gate import validate_plan_for_agent_spawn

    gate_result = await asyncio.to_thread(
        validate_plan_for_agent_spawn,
        agent_lookup_name,
        task_id,
        task_manager,
        code_index=code_index,
    )
    if gate_result is not None:
        if not gate_result.get("success"):
            return gate_result
        prompt_append = gate_result.get("prompt_append")
        if isinstance(prompt_append, str):
            prompt = f"{prompt}\n\n{prompt_append}"

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

    explicit_provider = concrete_provider(provider)
    default_provider = spawning_session_provider(
        session_manager,
        caller_session_id=caller_session_id,
        parent_session_id=parent_session_id,
    )
    agent_provider = agent_body.provider if agent_body else None
    try:
        effective_provider = resolve_spawn_provider(
            explicit_provider=explicit_provider,
            agent_provider=agent_provider,
            default_provider=default_provider,
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    provider_was_overridden = explicit_provider is not None

    provider_differs_from_agent = False
    if provider_was_overridden and agent_body:
        concrete_agent_provider = concrete_provider(agent_body.provider)
        provider_differs_from_agent = (
            concrete_agent_provider is not None and effective_provider != concrete_agent_provider
        )

    effective_model = _normalize_optional_model(model)
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
    )
    if reasoning.reasoning_required and reasoning.effective_effort is None:
        return {
            "success": False,
            "error": reasoning.message or "Requested reasoning is not supported",
            "reasoning": reasoning.to_dict(),
        }

    speed = resolve_spawn_speed(
        provider=effective_provider,
        model=effective_model,
        speed_mode=speed_mode,
    )
    speed_payload = speed_result(speed)

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

    requested_model_selector = effective_model
    from gobby.mcp_proxy.tools.spawn_agent._generation_endpoint import (
        resolve_spawn_generation_endpoint,
    )

    try:
        endpoint_resolution = await resolve_spawn_generation_endpoint(
            model=effective_model,
            api_base=effective_api_base,
            api_token=effective_api_token,
            daemon_config=daemon_config,
            run_manager=runner.run_storage,
            runtime_provider=effective_provider,
        )
    except ValueError as e:
        return {"success": False, "error": str(e), "speed": speed_payload}
    effective_model = endpoint_resolution.model
    effective_api_base = endpoint_resolution.api_base
    effective_api_token = endpoint_resolution.api_token
    is_local_run = endpoint_resolution.is_local
    try:
        speed_application = apply_speed(
            speed,
            model=effective_model,
            codex_config_overrides=endpoint_resolution.codex_config_overrides,
        )
    except SpeedUnavailableError as e:
        return {
            "success": False,
            "error": str(e),
            "reasoning": reasoning.to_dict(),
            "speed": e.speed,
        }
    effective_model = speed_application.model

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
        if not (allow_closed_task and is_task_reviewable(resolved_task)):
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
            logger.exception("Failed to prepare environment: %s", e)
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
    code_index_mode = code_index_preflight_mode(
        isolation=effective_isolation,
        agent_name=requested_agent_name,
        initial_variables=initial_variables,
        task_category=task_category,
    )

    # 8. Build enhanced prompt with isolation context
    enhanced_prompt = context_handler.build_context_prompt(prompt, isolation_ctx)

    run_id = str(uuid.uuid4())
    prepared_spawn = None
    spawn_request = None

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
    additional_skills = _normalize_string_list(effective_initial_variables.get("additional_skills"))
    if task_additional_skills is not None:
        additional_skills = task_additional_skills
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

    # 11. Build resume metadata without seeding the digest-owned session title.
    agent_display_name = requested_agent_name

    stage_name = effective_initial_variables.get("stage_name")
    stage_state = effective_initial_variables.get("stage_state")
    resume_metadata = build_resume_metadata(
        provider=effective_provider,
        model=requested_model_selector,
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
        child_session_manager = runner.child_session_manager
        if child_session_manager is None:
            task_spawn_lease.release_unattached()
            return {"success": False, "error": "Session manager is required to spawn an agent"}
        try:
            prepared_spawn = prepare_terminal_spawn(
                session_manager=child_session_manager,
                parent_session_id=parent_session_id,
                project_id=project_id,
                machine_id=get_machine_id(),
                source=effective_provider,
                workflow_name=effective_workflow,
                initial_variables=effective_initial_variables,
                prompt=enhanced_prompt,
                max_agent_depth=5,
                git_branch=isolation_ctx.branch_name,
                agent_run_id=run_id,
                task_id=resolved_task_id,
                claimed_session_id=claimed_session_id,
                agent_name=agent_display_name,
                model=effective_model,
                is_local=is_local_run,
                timeout_seconds=effective_timeout,
                sandbox_enabled=False,
                requested_reasoning_effort=reasoning.requested_effort,
                effective_reasoning_effort=reasoning.effective_effort,
                reasoning_required=reasoning.reasoning_required,
                reasoning_status=reasoning.status,
                reasoning_message=reasoning.message,
                resume_metadata_json=resume_metadata,
            )
        except Exception as exc:
            task_spawn_lease.release_unattached()
            await cleanup_created_isolation(
                handler, spawn_config, cleanup=cleanup_isolation_on_failure
            )
            return {
                "success": False,
                "error": str(exc),
                "reasoning": reasoning.to_dict(),
                "speed": speed_payload,
            }
        if db is not None and agent_body is not None and agent_body.step_workflow is not None:
            try:
                persist_initial_step_instance_if_resolved(
                    db,
                    agent_body,
                    session_id=prepared_spawn.session_id,
                    project_id=project_id,
                    initial_variables=effective_initial_variables,
                )
            except Exception as exc:
                cleanup_unlaunched_spawn(
                    child_session_manager,
                    session_id=prepared_spawn.session_id,
                    agent_run_id=prepared_spawn.agent_run_id,
                    prompt_file=prepared_spawn.prompt_file,
                    managed_credential=prepared_spawn.managed_credential,
                )
                task_spawn_lease.release_unattached()
                await cleanup_created_isolation(
                    handler, spawn_config, cleanup=cleanup_isolation_on_failure
                )
                return {
                    "success": False,
                    "error": str(exc),
                    "reasoning": reasoning.to_dict(),
                    "speed": speed_payload,
                }
        spawn_request = SpawnRequest(
            prompt=enhanced_prompt,
            cwd=isolation_ctx.cwd,
            provider=effective_provider,
            session_id=prepared_spawn.session_id,
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
            agent_name=agent_display_name,
            session_manager=child_session_manager,
            run_manager=runner.run_storage,
            machine_id=get_machine_id(),
            model=effective_model,
            is_local=is_local_run,
            codex_oss_provider=endpoint_resolution.codex_oss_provider,
            codex_config_overrides=speed_application.codex_config_overrides,
            api_base=effective_api_base,
            api_token=effective_api_token,
            requested_reasoning_effort=reasoning.requested_effort,
            effective_reasoning_effort=reasoning.effective_effort,
            reasoning_required=reasoning.reasoning_required,
            reasoning_status=reasoning.status,
            reasoning_message=reasoning.message,
            speed_resolution=speed,
            sandbox_config=effective_sandbox_config,
            extra_env={
                **(endpoint_resolution.child_env or {}),
            }
            or None,
            timeout_seconds=effective_timeout,
            daemon_config=daemon_config,
            resume_metadata_json=resume_metadata,
            code_index_preflight_mode=code_index_mode,
            code_index_api_token=read_local_api_token(),
            prepared_spawn=prepared_spawn,
        )
        try:
            spawn_result = await execute_spawn(spawn_request)
            remember_spawn_pid(spawn_result.pid, run_id=run_id)
        except Exception as exc:
            cleanup_unlaunched_spawn(
                child_session_manager,
                session_id=prepared_spawn.session_id,
                agent_run_id=prepared_spawn.agent_run_id,
                prompt_file=prepared_spawn.prompt_file,
                managed_credential=prepared_spawn.managed_credential,
            )
            task_spawn_lease.release_unattached()
            await cleanup_failed_spawn(
                runner,
                run_id,
                str(exc),
                handler,
                spawn_config,
                completion_registry=completion_registry,
                cleanup_isolation=cleanup_isolation_on_failure,
                task_manager=task_manager,
                child_session_id=prepared_spawn.session_id,
            )
            return {
                "success": False,
                "error": str(exc),
                "reasoning": reasoning.to_dict(),
                "speed": speed_payload,
            }
        tmux_session_name, tmux_socket_name, tmux_socket_path = _tmux_runtime_metadata(spawn_result)
        _persist_spawn_runtime(
            runner,
            run_id,
            spawn_result,
            tmux_session_name=tmux_session_name,
            worktree_id=isolation_ctx.worktree_id,
            clone_id=isolation_ctx.clone_id,
        )
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
                    completion_registry=completion_registry,
                    cleanup_isolation=cleanup_isolation_on_failure,
                    task_manager=task_manager,
                    child_session_id=spawn_result.child_session_id,
                    pid=spawn_result.pid,
                    tmux_session_name=tmux_session_name,
                    tmux_socket_name=tmux_socket_name,
                    tmux_socket_path=tmux_socket_path,
                )
                return {
                    "success": False,
                    "error": error,
                    "run_id": run_id,
                    "speed": speed_payload,
                }
    tmux_session_name, tmux_socket_name, tmux_socket_path = _tmux_runtime_metadata(spawn_result)

    tmux_spawn = bool(
        spawn_result.success and spawn_result.terminal_type == "tmux" and tmux_session_name
    )
    if tmux_spawn and tmux_session_name:
        alive, pane_output = await _check_tmux_session_alive(
            tmux_session_name,
            socket_name=tmux_socket_name,
            socket_path=tmux_socket_path,
        )
        if not alive:
            spawn_result.success = False
            spawn_result.status = "failed"
            spawn_result.error = f"tmux session '{tmux_session_name}' failed live-pane verification"
            if pane_output:
                spawn_result.error = f"{spawn_result.error}\nPane output:\n{pane_output}"
            await cleanup_failed_spawn(
                runner,
                run_id,
                spawn_result.error,
                handler,
                spawn_config,
                completion_registry=completion_registry,
                cleanup_isolation=cleanup_isolation_on_failure,
                task_manager=task_manager,
                child_session_id=spawn_result.child_session_id,
                pid=spawn_result.pid,
                tmux_session_name=tmux_session_name,
                tmux_socket_name=tmux_socket_name,
                tmux_socket_path=tmux_socket_path,
            )
            return {
                "success": False,
                "error": spawn_result.error,
                "run_id": run_id,
                "speed": speed_payload,
            }

    if spawn_result.success and spawn_result.child_session_id is not None:
        start_error = await start_run_or_cleanup(
            runner,
            run_id,
            handler,
            spawn_config,
            completion_registry=completion_registry,
            cleanup_isolation=cleanup_isolation_on_failure,
            task_manager=task_manager,
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
                    if (
                        task_owned_by_child
                        and db is not None
                        and agent_body is not None
                        and agent_body.step_workflow is not None
                    ):
                        apply_claimed_step_update(
                            db,
                            agent_body,
                            session_id=spawn_result.child_session_id,
                            initial_variables=effective_initial_variables,
                        )
            except Exception as e:
                error = f"Failed to auto-claim task {resolved_task_id}: {e}"
                logger.warning(error)
                await cleanup_failed_spawn(
                    runner,
                    run_id,
                    error,
                    handler,
                    spawn_config,
                    completion_registry=completion_registry,
                    cleanup_isolation=cleanup_isolation_on_failure,
                    task_manager=task_manager,
                    child_session_id=spawn_result.child_session_id,
                    pid=spawn_result.pid,
                    tmux_session_name=tmux_session_name,
                    tmux_socket_name=tmux_socket_name,
                    tmux_socket_path=tmux_socket_path,
                )
                return {
                    "success": False,
                    "error": error,
                    "run_id": run_id,
                    "speed": speed_payload,
                }

        # Post-spawn health check: verify tmux session is still alive.
        if spawn_result.terminal_type == "tmux" and tmux_session_name:
            schedule_tmux_health_check(
                runner,
                run_id,
                tmux_session_name,
                tmux_socket_name,
                tmux_socket_path,
                completion_registry,
            )
    else:
        task_spawn_lease.release_unattached()
        await cleanup_failed_spawn(
            runner,
            run_id,
            spawn_result.error or "Spawn failed",
            handler,
            spawn_config,
            completion_registry=completion_registry,
            cleanup_isolation=cleanup_isolation_on_failure,
            task_manager=task_manager,
            child_session_id=spawn_result.child_session_id,
            pid=spawn_result.pid,
            tmux_session_name=tmux_session_name,
            tmux_socket_name=tmux_socket_name,
            tmux_socket_path=tmux_socket_path,
        )

    # 13. Return response with isolation metadata
    if not spawn_result.success:
        return {
            "success": False,
            "error": spawn_result.error or "Failed to spawn agent",
            "reasoning": reasoning.to_dict(),
            "speed": speed_payload,
        }

    response = _build_spawn_success_response(
        run_id=run_id,
        spawn_result=spawn_result,
        effective_isolation=effective_isolation,
        isolation_ctx=isolation_ctx,
        base_commit_sha=base_commit_sha,
        tmux_session_name=tmux_session_name,
        tmux_socket_name=tmux_socket_name,
        tmux_socket_path=tmux_socket_path,
        code_index_preflight_warning=(
            spawn_request.code_index_preflight_warning if spawn_request is not None else None
        ),
        reasoning=reasoning,
    )
    response["speed"] = speed_payload
    return response
