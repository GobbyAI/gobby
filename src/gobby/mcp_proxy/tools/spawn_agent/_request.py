"""Assemble a managed launch request from resolved spawn context."""

from __future__ import annotations

from typing import Any, Literal

from gobby.agents.isolation import IsolationContext, SpawnConfig
from gobby.agents.reasoning import SpawnReasoningResolution
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_models import ManagedRuntimeProfile, SpawnRequest

from ._generation_endpoint import SpawnGenerationEndpointResolution


def build_spawn_request(
    *,
    prompt: str,
    spawn_config: SpawnConfig,
    isolation_ctx: IsolationContext,
    prepared_spawn: PreparedSpawn,
    runner: Any,
    run_id: str,
    workflow: str | None,
    initial_variables: dict[str, Any],
    claimed_session_id: str | None,
    agent_name: str | None,
    machine_id: str | None,
    endpoint: SpawnGenerationEndpointResolution,
    reasoning: SpawnReasoningResolution,
    sandbox_config: SandboxConfig,
    timeout: float | None,
    daemon_config: Any,
    resume_metadata: dict[str, Any],
    code_index_mode: str | None,
    code_index_api_token: str | None,
    phase_timings_ms: dict[str, float],
    terminal_backend: Literal["tmux", "native"],
    managed_runtime_profile: ManagedRuntimeProfile | None = None,
) -> SpawnRequest:
    """Keep request construction separate from allocation and launch scheduling."""
    return SpawnRequest(
        prompt=prompt,
        cwd=isolation_ctx.cwd,
        provider=spawn_config.provider,
        managed_runtime_profile=managed_runtime_profile,
        session_id=prepared_spawn.session_id,
        run_id=run_id,
        agent_run_id=run_id,
        parent_session_id=spawn_config.parent_session_id,
        project_id=spawn_config.project_id,
        project_path=spawn_config.project_path,
        workflow=workflow,
        initial_variables=initial_variables,
        worktree_id=isolation_ctx.worktree_id,
        clone_id=isolation_ctx.clone_id,
        branch_name=isolation_ctx.branch_name,
        task_id=spawn_config.task_id,
        claimed_session_id=claimed_session_id,
        agent_name=agent_name,
        session_manager=runner.child_session_manager,
        run_manager=runner.run_storage,
        machine_id=machine_id,
        model=endpoint.model,
        is_local=endpoint.is_local,
        codex_oss_provider=endpoint.codex_oss_provider,
        codex_config_overrides=endpoint.codex_config_overrides,
        api_base=endpoint.api_base,
        api_token=endpoint.api_token,
        requested_reasoning_effort=reasoning.requested_effort,
        effective_reasoning_effort=reasoning.effective_effort,
        reasoning_required=reasoning.reasoning_required,
        reasoning_status=reasoning.status,
        reasoning_message=reasoning.message,
        auto_approve=(
            managed_runtime_profile.auto_approve if managed_runtime_profile is not None else True
        ),
        provider_args=(
            managed_runtime_profile.provider_args if managed_runtime_profile is not None else ()
        ),
        sandbox_config=sandbox_config,
        extra_env={
            **(endpoint.child_env or {}),
        }
        or None,
        timeout_seconds=timeout,
        daemon_config=daemon_config,
        resume_metadata_json=resume_metadata,
        code_index_preflight_mode=code_index_mode,
        code_index_api_token=code_index_api_token,
        prepared_spawn=prepared_spawn,
        phase_timings_ms=phase_timings_ms,
        terminal_manager=getattr(runner, "terminal_manager", None),
        terminal_runtime_registry=getattr(runner, "terminal_runtime_registry", None),
        write_coordinator=getattr(runner, "write_coordinator", None),
        terminal_backend=terminal_backend,
    )
