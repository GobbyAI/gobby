"""Coordinate managed spawn admission, allocation and launch scheduling."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from gobby.agents.cargo_target import cleanup_checkout_cargo_target_dir
from gobby.agents.completion_subscribers import subscribe_agent_completion
from gobby.agents.external_write_grants import GRANT_KEY, apply_write_grant, authorize_write_grant
from gobby.agents.isolation import (
    CloneIsolationHandler,
    IsolationHandler,
    SpawnConfig,
    WorktreeIsolationHandler,
    get_isolation_handler,
    provider_mcp_config_error,
    repair_isolation_environment,
)
from gobby.agents.provider_rotation import model_for_provider
from gobby.agents.reasoning import resolve_spawn_reasoning
from gobby.agents.sandbox import SandboxConfig, agent_sandbox_config
from gobby.agents.spawn import prepare_terminal_spawn
from gobby.agents.spawn_executor import execute_spawn
from gobby.agents.spawn_executor_providers import agy_support_refusal
from gobby.agents.spawn_models import ManagedRuntimeProfile, resolve_terminal_backend
from gobby.agents.spawn_timing import finish_spawn_phase, start_spawn_phase
from gobby.agents.worktree_reuse import ReusedWorktreeRebaseConflict
from gobby.mcp_proxy.tools._background_task_lifecycle import schedule_background_task
from gobby.providers.version_gate import (
    AGY_REVALIDATING_REASON,
    AGY_UNPUBLISHED_REASON,
    ensure_agy_support,
    peek_agy_support,
)
from gobby.utils.local_token import read_local_api_token
from gobby.utils.machine_id import get_machine_id
from gobby.utils.project_context import get_project_context
from gobby.workflows.definitions import AgentDefinitionBody

from ._code_index import code_index_preflight_mode
from ._execution import finalize_executed_spawn
from ._failure_cleanup import (
    cleanup_created_isolation,
    cleanup_failed_spawn,
    remember_spawn_pid,
)
from ._managed_runtime import (
    managed_runtime_pair_error,
    managed_runtime_path_error,
    managed_runtime_selection_error,
)
from ._provider_resolution import (
    concrete_provider,
    incompatible_spawn_model_provider_after_recollect,
    missing_provider_for_supplied_model,
    resolve_spawn_provider,
    spawning_session_provider,
)
from ._request import build_spawn_request
from ._runtime import (
    _normalize_optional_model,
    build_spawn_context,
)
from ._spawn_guards import (
    TaskSpawnLease,
    active_task_response_if_blocked,
    reserve_agent_slot,
    resolve_spawn_task_context,
)
from ._step_state import persist_initial_step_instance_if_resolved
from ._worktree_reuse import prepare_reused_worktree

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)

_spawn_background_tasks: dict[str, asyncio.Task[None]] = {}


async def spawn_agent_impl(
    prompt: str,
    runner: AgentRunner,
    agent_body: AgentDefinitionBody | None = None,
    agent_lookup_name: str | None = None,
    task_id: str | None = None,
    task_manager: LocalTaskManager | None = None,
    allow_closed_task: bool = False,
    isolation: Literal["none", "worktree", "clone"] | None = None,
    branch_name: str | None = None,
    base_branch: str | None = None,
    clone_id: str | None = None,  # Reuse existing clone instead of creating new isolation
    worktree_id: str | None = None,  # Reuse existing worktree instead of creating new isolation
    cleanup_isolation_on_failure: bool = False,
    worktree_storage: Any | None = None,
    git_manager: Any | None = None,
    git_manager_resolver: Callable[[str], Any | None] | None = None,
    clone_storage: Any | None = None,
    clone_manager: Any | None = None,
    workflow: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    reasoning_required: bool | None = None,
    timeout: float | None = None,
    parent_session_id: str | None = None,
    caller_session_id: str | None = None,
    project_path: str | None = None,
    target_project_id: str | None = None,
    initial_variables: dict[str, Any] | None = None,
    session_manager: Any | None = None,  # SessionManager
    db: Any | None = None,  # HubDatabase
    completion_registry: Any | None = None,
    notify_parent_on_completion: bool = False,
    daemon_config: Any | None = None,  # DaemonConfig
    code_index: Any | None = None,  # CodeIndexContext
    held_task_mutex: Any | None = None,
    terminal_backend: Literal["tmux", "native"] | None = None,
    droid_mode: Literal["exec", "interactive"] = "exec",
    managed_runtime_profile: ManagedRuntimeProfile | None = None,
    prelaunch_authority: Callable[[str], None] | None = None,
    extra_write_paths: list[str] | None = None,
    write_paths_reason: str | None = None,
    reserved_run_id: str | None = None,
) -> dict[str, Any]:
    """Core spawn_agent implementation used by the MCP tool and direct callers."""
    try:
        write_grant = await asyncio.to_thread(
            authorize_write_grant,
            extra_write_paths,
            write_paths_reason,
            caller_session_id=caller_session_id,
            parent_session_id=parent_session_id,
            session_manager=session_manager,
            run_storage=runner.run_storage,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if agent_body is not None:
        try:
            agent_body.prompt_for("agent")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    try:
        resolved_terminal_backend = resolve_terminal_backend(terminal_backend, daemon_config)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    managed_error = managed_runtime_pair_error(managed_runtime_profile, prelaunch_authority)
    if managed_error:
        return {"success": False, "error": managed_error}
    if managed_runtime_profile is not None and write_grant:
        return {"success": False, "error": "ask_external_write_grant_forbidden"}
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
    missing_provider = missing_provider_for_supplied_model(
        explicit_provider=explicit_provider,
        model=model,
    )
    if missing_provider is not None:
        return missing_provider.to_response()
    default_provider = await asyncio.to_thread(
        spawning_session_provider,
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
    managed_error = managed_runtime_selection_error(
        managed_runtime_profile,
        isolation=effective_isolation,
        provider=effective_provider,
    )
    if managed_error:
        return {"success": False, "error": managed_error}
    if effective_provider == "agy":
        # Gate on the published support record before any isolation, slot,
        # session, or agent-run side effect exists to clean up.
        agy_record = peek_agy_support()
        if not agy_record.supported and agy_record.reason in {
            AGY_REVALIDATING_REASON,
            AGY_UNPUBLISHED_REASON,
        }:
            agy_record = await ensure_agy_support()
        if not agy_record.supported:
            return {"success": False, "error": agy_support_refusal(agy_record)}
    effective_model = _normalize_optional_model(model)
    if effective_model is None and agent_body:
        agent_model = _normalize_optional_model(agent_body.model)
        if agent_model is not None:
            # The agent names one model, but the provider it lands on need not be
            # the one that model belongs to: the spawn may override the provider,
            # and `provider: inherit` follows the spawning session. Resolve the
            # target provider's model at the same tier rather than leaving the
            # model unset for that CLI's configured default to fill in.
            effective_model = model_for_provider(
                target_provider=effective_provider,
                declared_model=agent_model,
            )
            if effective_model is None:
                return {
                    "success": False,
                    "error": (
                        f"Agent {agent_body.name!r} declares model {agent_model!r}, which "
                        f"belongs to another provider and has no {effective_provider} "
                        f"equivalent. Pass an explicit model for {effective_provider} "
                        "instead of leaving it to the provider's default."
                    ),
                }
    pair_error = await incompatible_spawn_model_provider_after_recollect(
        provider=effective_provider,
        model=effective_model,
    )
    if pair_error is not None:
        return pair_error.to_response()
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

    if managed_runtime_profile is not None:
        try:
            managed_runtime_profile.validate_selection(
                provider=effective_provider,
                model=effective_model,
                reasoning_effort=requested_reasoning_effort,
                api_base=effective_api_base,
            )
        except (RuntimeError, ValueError) as selection_error:
            return {"success": False, "error": str(selection_error)}

    requested_model_selector = effective_model
    machine_id = await asyncio.to_thread(get_machine_id)
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
            machine_id=machine_id,
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}
    effective_model = endpoint_resolution.model
    effective_api_base = endpoint_resolution.api_base
    effective_api_token = endpoint_resolution.api_token
    is_local_run = endpoint_resolution.is_local

    effective_timeout = timeout
    if effective_timeout is None and agent_body and agent_body.timeout:
        effective_timeout = agent_body.timeout
    if effective_timeout == 0:
        effective_timeout = None  # 0 means no timeout

    effective_workflow = workflow
    ctx = await asyncio.to_thread(get_project_context, Path(project_path) if project_path else None)
    if ctx is None and target_project_id is None:
        return {"success": False, "error": "Could not resolve project context"}

    context_project_id = (ctx.get("id") or ctx.get("project_id")) if ctx else None
    project_id = target_project_id or context_project_id
    resolved_project_path = ctx.get("project_path") if ctx else project_path

    if not project_id or not isinstance(project_id, str):
        return {"success": False, "error": "Could not resolve project_id from context"}

    target_git_manager = git_manager
    if git_manager_resolver is not None:
        try:
            target_git_manager = git_manager_resolver(project_id)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "success": False,
                "error": f"Could not resolve Git manager for project '{project_id}': {exc}",
            }
        if target_git_manager is None:
            return {
                "success": False,
                "error": f"No Git manager available for project '{project_id}'",
            }

    manager_repo_path = getattr(target_git_manager, "repo_path", None)
    if git_manager_resolver is not None and manager_repo_path is not None:
        if effective_isolation != "none" or not resolved_project_path:
            resolved_project_path = str(manager_repo_path)

    if not resolved_project_path or not isinstance(resolved_project_path, str):
        return {"success": False, "error": "Could not resolve project_path from context"}
    managed_error = managed_runtime_path_error(managed_runtime_profile, resolved_project_path)
    if managed_error:
        return {"success": False, "error": managed_error}

    target_clone_manager = clone_manager
    if target_git_manager is not None and (effective_isolation == "clone" or clone_id):
        try:
            from gobby.clones.git import CloneGitManager

            target_clone_manager = CloneGitManager(target_git_manager.repo_path)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "success": False,
                "error": f"Could not create clone manager for project '{project_id}': {exc}",
            }

    effective_base_branch = base_branch
    if effective_base_branch is None and agent_body:
        effective_base_branch = agent_body.base_branch
    # "inherit" means "resolve from context", treat as unset
    if effective_base_branch == "inherit":
        effective_base_branch = None
    if effective_base_branch is None and target_git_manager:
        try:
            effective_base_branch = await target_git_manager.get_current_branch()
        except Exception as e:
            logger.debug("Failed to auto-detect current branch: %s", e, exc_info=True)
            effective_base_branch = None
    effective_base_branch = effective_base_branch or "main"
    # Daemon-owned agent sandboxes inherit from config-store defaults only.
    effective_sandbox_config: SandboxConfig = (
        managed_runtime_profile.sandbox_config
        if managed_runtime_profile is not None
        else apply_write_grant(agent_sandbox_config(daemon_config), write_grant)
    )
    requested_agent_name = agent_lookup_name or (agent_body.name if agent_body else None)
    if not parent_session_id:
        return {"success": False, "error": "parent_session_id is required"}

    can_spawn, reason, _depth = await asyncio.to_thread(runner.can_spawn, parent_session_id)
    if not can_spawn:
        return {"success": False, "error": reason}
    task_context = await resolve_spawn_task_context(
        prompt=prompt,
        task_id=task_id,
        task_manager=task_manager,
        project_id=project_id,
        allow_closed_task=allow_closed_task,
        agent_body=agent_body,
        initial_variables=initial_variables,
    )
    if task_context.refusal is not None:
        return task_context.refusal
    prompt = task_context.prompt
    resolved_task_id = task_context.resolved_task_id
    task_title = task_context.task_title
    task_seq_num = task_context.task_seq_num
    task_category = task_context.task_category
    task_additional_skills = task_context.task_additional_skills
    claimed_session_id = task_context.claimed_session_id
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
    if droid_mode == "interactive" and effective_provider != "droid":
        return {
            "success": False,
            "error": "droid_mode='interactive' requires provider='droid'",
        }
    isolation_ctx = None
    if worktree_id and worktree_storage:
        try:
            worktree_id = worktree_storage.resolve_reference(worktree_id)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        existing_worktree = worktree_storage.get(worktree_id)
        if not existing_worktree:
            return {"success": False, "error": f"Worktree {worktree_id} not found"}
        if not Path(existing_worktree.worktree_path).is_dir():
            cargo_error = await asyncio.to_thread(
                cleanup_checkout_cargo_target_dir,
                Path(existing_worktree.worktree_path),
                existing_worktree.project_id,
            )
            if cargo_error is not None:
                return {
                    "success": False,
                    "error_code": "cargo_target_cleanup_failed",
                    "error": (
                        "Worktree directory was already missing, but Cargo target cleanup "
                        f"failed: {cargo_error}"
                    ),
                }
            worktree_storage.delete(worktree_id)
            return {
                "success": False,
                "error": f"Worktree directory missing: {existing_worktree.worktree_path} (stale record cleaned up)",
            }

        if target_git_manager is None:
            return {"success": False, "error": "git_manager is required to reuse a worktree"}

        try:
            isolation_ctx, handler = await prepare_reused_worktree(
                existing_worktree=existing_worktree,
                git_manager=target_git_manager,
                worktree_storage=worktree_storage,
                spawn_config=spawn_config,
                main_repo_path=resolved_project_path,
            )
            effective_isolation = "worktree"
            context_handler: IsolationHandler = WorktreeIsolationHandler(
                target_git_manager, worktree_storage
            )
        except ReusedWorktreeRebaseConflict as exc:
            return {
                "success": False,
                "error_code": "reused_worktree_rebase_conflict",
                "error": (
                    f"Reused worktree {existing_worktree.id} could not be rebased onto "
                    f"{exc.base_ref}; the original worktree was preserved at "
                    f"{existing_worktree.worktree_path}: {exc}"
                ),
                "worktree_id": existing_worktree.id,
                "worktree_path": existing_worktree.worktree_path,
                "branch_name": existing_worktree.branch_name,
                "base_branch": existing_worktree.base_branch,
                "rebase_target": exc.base_ref,
                "base_commit_sha": exc.base_commit_sha,
                "preserved": True,
                "recovery": (
                    "Check the preserved worktree's rebase state and resolve the branch against "
                    f"'{exc.base_ref}' in the preserved worktree, then retry spawn_agent "
                    f"with worktree_id='{existing_worktree.id}'."
                ),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to prepare reused worktree: {e}"}
    elif clone_id and clone_storage:
        existing_clone = clone_storage.get(clone_id)
        if not existing_clone:
            return {"success": False, "error": f"Clone {clone_id} not found"}
        if not Path(existing_clone.clone_path).is_dir():
            cargo_error = await asyncio.to_thread(
                cleanup_checkout_cargo_target_dir,
                Path(existing_clone.clone_path),
                existing_clone.project_id,
            )
            if cargo_error is not None:
                return {
                    "success": False,
                    "error_code": "cargo_target_cleanup_failed",
                    "error": (
                        "Clone directory was already missing, but Cargo target cleanup failed: "
                        f"{cargo_error}"
                    ),
                }
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
        context_handler = CloneIsolationHandler(
            target_clone_manager, clone_storage, target_git_manager
        )
    else:
        handler = get_isolation_handler(
            effective_isolation,
            git_manager=target_git_manager,
            worktree_storage=worktree_storage,
            clone_manager=target_clone_manager,
            clone_storage=clone_storage,
        )
        context_handler = handler

    if isolation_ctx is None:
        try:
            isolation_ctx = await handler.prepare_environment(spawn_config)
        except Exception as e:
            logger.exception("Failed to prepare environment: %s", e)
            try:
                await handler.cleanup_environment(spawn_config)
            except Exception as cleanup_err:
                logger.warning("Cleanup after prepare failure also failed: %s", cleanup_err)
            response: dict[str, Any] = {
                "success": False,
                "error": f"Failed to prepare environment: {e}",
            }
            error_code = getattr(e, "code", None)
            if isinstance(error_code, str):
                response["error_code"] = error_code
            return response

    if (
        managed_runtime_profile is not None
        and Path(isolation_ctx.cwd).resolve()
        != Path(managed_runtime_profile.scratch_root).resolve()
    ):
        await cleanup_created_isolation(handler, spawn_config, cleanup=cleanup_isolation_on_failure)
        return {
            "success": False,
            "error": "managed runtime cwd does not match immutable scratch root",
        }

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
    enhanced_prompt = context_handler.build_context_prompt(prompt, isolation_ctx)

    run_id = reserved_run_id or str(uuid.uuid4())
    prepared_spawn = None
    spawn_request = None

    agent_display_name = requested_agent_name
    base_commit_sha = isolation_ctx.extra.get("base_commit_sha")
    effective_initial_variables, resume_metadata = await build_spawn_context(
        spawn_config=spawn_config,
        isolation_ctx=isolation_ctx,
        effective_isolation=effective_isolation,
        reasoning=reasoning,
        initial_variables=initial_variables,
        local_context_route=endpoint_resolution.local_context_route,
        local_context_observation=endpoint_resolution.local_context_observation,
        session_manager=session_manager,
        task_additional_skills=task_additional_skills,
        enhanced_prompt=enhanced_prompt,
        requested_model_selector=requested_model_selector,
        effective_sandbox_config=effective_sandbox_config,
        effective_workflow=effective_workflow,
        agent_display_name=agent_display_name,
    )

    if write_grant:
        resume_metadata[GRANT_KEY] = write_grant

    task_spawn_lease = TaskSpawnLease(
        db=db,
        task_id=resolved_task_id,
        held_mutex=held_task_mutex,
    )
    if resolved_task_id and runner.run_storage:
        active_response = await asyncio.to_thread(
            active_task_response_if_blocked,
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
    lease_response = await asyncio.to_thread(task_spawn_lease.acquire)
    if lease_response is not None:
        await cleanup_created_isolation(handler, spawn_config, cleanup=cleanup_isolation_on_failure)
        return lease_response
    if resolved_task_id and runner.run_storage:
        active_response = await asyncio.to_thread(
            active_task_response_if_blocked,
            run_storage=runner.run_storage,
            task_id=resolved_task_id,
            task_ref=task_id,
            requested_agent_name=requested_agent_name,
            parent_session_id=parent_session_id,
        )
        if active_response is not None:
            await asyncio.to_thread(task_spawn_lease.release_unattached)
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
            await asyncio.to_thread(task_spawn_lease.release_unattached)
            await cleanup_created_isolation(
                handler, spawn_config, cleanup=cleanup_isolation_on_failure
            )
            return slot_response
        child_session_manager = runner.child_session_manager
        if child_session_manager is None:
            await asyncio.to_thread(task_spawn_lease.release_unattached)
            await cleanup_created_isolation(
                handler, spawn_config, cleanup=cleanup_isolation_on_failure
            )
            return {"success": False, "error": "Session manager is required to spawn an agent"}
        phase_timings_ms: dict[str, float] = {}
        prepare_terminal_started = start_spawn_phase()
        try:
            # Child-session creation, run persistence, credential-role issuance, and
            # grant materialization are synchronous. PostgreSQL pool acquisition alone
            # can wait for its full timeout, so keep the complete transactional
            # preparation chain off the daemon event loop.
            prepared_spawn = await asyncio.to_thread(
                prepare_terminal_spawn,
                session_manager=child_session_manager,
                credential_manager=runner.run_storage.credential_manager,
                parent_session_id=parent_session_id,
                project_id=project_id,
                machine_id=machine_id,
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
                worktree_id=isolation_ctx.worktree_id,
                clone_id=isolation_ctx.clone_id,
                workspace_path=str(isolation_ctx.cwd),
            )
        except Exception as exc:
            await asyncio.to_thread(task_spawn_lease.release_unattached)
            await cleanup_created_isolation(
                handler, spawn_config, cleanup=cleanup_isolation_on_failure
            )
            return {
                "success": False,
                "error": str(exc),
                "reasoning": reasoning.to_dict(),
            }
        finally:
            finish_spawn_phase(
                phase_timings_ms,
                "prepare_terminal_spawn",
                prepare_terminal_started,
            )
        spawn_identity = {
            "run_id": run_id,
            "worktree_id": isolation_ctx.worktree_id,
            "branch_name": isolation_ctx.branch_name,
        }
        if prelaunch_authority is not None:
            try:
                await asyncio.to_thread(prelaunch_authority, prepared_spawn.agent_run_id)
            except Exception as exc:
                await asyncio.to_thread(task_spawn_lease.release_unattached)
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
                    **spawn_identity,
                    "reasoning": reasoning.to_dict(),
                }
        attach_error = await asyncio.to_thread(task_spawn_lease.attach, run_id)
        if attach_error is not None:
            await asyncio.to_thread(task_spawn_lease.release_unattached)
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
                child_session_id=prepared_spawn.session_id,
            )
            return {
                "success": False,
                "error": error,
                **spawn_identity,
                "reasoning": reasoning.to_dict(),
            }
        if db is not None and agent_body is not None and agent_body.step_workflow is not None:
            try:
                await asyncio.to_thread(
                    persist_initial_step_instance_if_resolved,
                    db,
                    agent_body,
                    session_id=prepared_spawn.session_id,
                    project_id=project_id,
                    initial_variables=effective_initial_variables,
                )
            except Exception as exc:
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
                    **spawn_identity,
                    "reasoning": reasoning.to_dict(),
                }
        spawn_request = build_spawn_request(
            prompt=enhanced_prompt,
            managed_runtime_profile=managed_runtime_profile,
            spawn_config=spawn_config,
            isolation_ctx=isolation_ctx,
            prepared_spawn=prepared_spawn,
            runner=runner,
            run_id=run_id,
            workflow=effective_workflow,
            initial_variables=effective_initial_variables,
            claimed_session_id=claimed_session_id,
            agent_name=agent_display_name,
            machine_id=machine_id,
            endpoint=endpoint_resolution,
            reasoning=reasoning,
            sandbox_config=effective_sandbox_config,
            timeout=effective_timeout,
            daemon_config=daemon_config,
            resume_metadata=resume_metadata,
            code_index_mode=code_index_mode,
            code_index_api_token=await asyncio.to_thread(read_local_api_token),
            phase_timings_ms=phase_timings_ms,
            terminal_backend=resolved_terminal_backend,
            droid_mode=droid_mode,
        )

        async def _spawn_failure(error: str, *, infrastructure: bool = False) -> dict[str, Any]:
            if infrastructure:
                await asyncio.to_thread(
                    runner.run_storage.merge_resume_metadata,
                    run_id,
                    {"spawn_retryable_infrastructure": True},
                )
            await cleanup_failed_spawn(
                runner,
                run_id,
                error,
                handler,
                spawn_config,
                completion_registry=completion_registry,
                cleanup_isolation=cleanup_isolation_on_failure,
                task_manager=task_manager,
                child_session_id=prepared_spawn.session_id,
            )
            return {
                "success": False,
                "error": error,
                **spawn_identity,
                "reasoning": reasoning.to_dict(),
            }

        async def _execute_spawn_phase() -> dict[str, Any]:
            try:
                spawn_result = await execute_spawn(spawn_request)
                await asyncio.to_thread(remember_spawn_pid, spawn_result.pid, run_id=run_id)
                return await finalize_executed_spawn(
                    runner=runner,
                    run_id=run_id,
                    spawn_result=spawn_result,
                    spawn_request=spawn_request,
                    isolation_ctx=isolation_ctx,
                    effective_isolation=effective_isolation,
                    base_commit_sha=base_commit_sha,
                    handler=handler,
                    spawn_config=spawn_config,
                    completion_registry=completion_registry,
                    cleanup_isolation_on_failure=cleanup_isolation_on_failure,
                    task_manager=task_manager,
                    session_manager=session_manager,
                    parent_session_id=parent_session_id,
                    effective_provider=effective_provider,
                    resolved_task_id=resolved_task_id,
                    task_seq_num=task_seq_num,
                    db=db,
                    agent_body=agent_body,
                    effective_initial_variables=effective_initial_variables,
                    reasoning=reasoning,
                )
            except asyncio.CancelledError:
                await _spawn_failure("Agent spawn cancelled")
                raise
            except Exception as exc:
                return await _spawn_failure(str(exc), infrastructure=isinstance(exc, OSError))

        async def _run_spawn_phase() -> None:
            result = await _execute_spawn_phase()
            if not result.get("success"):
                logger.warning(
                    "Background agent boot failed for run %s: %s",
                    run_id,
                    result.get("error", "unknown error"),
                )

        if notify_parent_on_completion and completion_registry and parent_session_id:
            try:
                await asyncio.to_thread(
                    subscribe_agent_completion,
                    completion_registry=completion_registry,
                    run_id=run_id,
                    subscriber_session_id=parent_session_id,
                    db=db,
                    strict=managed_runtime_profile is not None,
                )
            except Exception:
                logger.warning(
                    "Failed to subscribe parent session to agent completion for run %s",
                    run_id,
                    exc_info=True,
                )

        try:
            schedule_background_task(
                _spawn_background_tasks,
                run_id,
                _run_spawn_phase,
                name=f"gobby-agent-spawn-{run_id}",
                logger=logger,
                description="Agent spawn background task",
            )
        except RuntimeError as exc:
            return await _spawn_failure(str(exc))

        return {
            "success": True,
            "status": "starting",
            GRANT_KEY: write_grant,
            **spawn_identity,
            "child_session_id": prepared_spawn.session_id,
            "isolation": effective_isolation,
            "clone_id": isolation_ctx.clone_id,
            "reasoning": reasoning.to_dict(),
        }
