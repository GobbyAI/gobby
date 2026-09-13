"""Runtime helpers for spawn_agent implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

from gobby.agents.isolation import IsolationContext, SpawnConfig
from gobby.agents.reasoning import SpawnReasoningResolution
from gobby.agents.resume_metadata import build_resume_metadata
from gobby.agents.sandbox import SandboxConfig

if TYPE_CHECKING:
    from gobby.providers.capabilities.local_context import LocalContextObservation
    from gobby.providers.capabilities.local_context_config import LocalContextRoute

logger = logging.getLogger(__name__)


async def build_spawn_context(
    *,
    spawn_config: SpawnConfig,
    isolation_ctx: IsolationContext,
    effective_isolation: str,
    reasoning: SpawnReasoningResolution,
    initial_variables: dict[str, Any] | None,
    local_context_route: LocalContextRoute | None,
    local_context_observation: LocalContextObservation | None,
    session_manager: Any | None,
    task_additional_skills: list[str] | None,
    enhanced_prompt: str,
    requested_model_selector: str | None,
    effective_sandbox_config: SandboxConfig,
    effective_workflow: str | None,
    agent_display_name: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build workflow variables and the matching durable launch snapshot."""
    # Preserve factory defaults before adding launch-owned variables.
    effective_initial_variables: dict[str, Any] = {}
    if initial_variables:
        effective_initial_variables.update(initial_variables)
    from gobby.sessions.context_usage import (
        LOCAL_CONTEXT_OBSERVATION_VARIABLE,
        LOCAL_CONTEXT_ROUTE_VARIABLE,
        local_context_variable_updates,
    )

    if local_context_route is not None:
        context_updates = local_context_variable_updates(
            local_context_route,
            local_context_observation,
        )
    else:
        context_updates = {}
        effective_initial_variables.pop(LOCAL_CONTEXT_ROUTE_VARIABLE, None)
        effective_initial_variables.pop(LOCAL_CONTEXT_OBSERVATION_VARIABLE, None)
    effective_initial_variables.update(context_updates)
    if reasoning.status != "not_requested":
        effective_initial_variables.update(
            {
                "_requested_reasoning_effort": reasoning.requested_effort,
                "_effective_reasoning_effort": reasoning.effective_effort,
                "_reasoning_required": reasoning.reasoning_required,
                "_reasoning_status": reasoning.status,
            }
        )
    if spawn_config.task_id:
        effective_initial_variables["assigned_task_id"] = (
            f"#{spawn_config.task_seq_num}" if spawn_config.task_seq_num else spawn_config.task_id
        )
        effective_initial_variables["assigned_task_uuid"] = spawn_config.task_id
    if "assigned_task_id" in effective_initial_variables:
        effective_initial_variables["parent_session_id"] = spawn_config.parent_session_id
        effective_initial_variables["parent_session_ref"] = await asyncio.to_thread(
            _parent_session_ref, session_manager, spawn_config.parent_session_id
        )
    if enhanced_prompt:
        effective_initial_variables["prompt"] = enhanced_prompt
    additional_skills = _normalize_string_list(effective_initial_variables.get("additional_skills"))
    if task_additional_skills is not None:
        additional_skills = task_additional_skills
    effective_initial_variables["additional_skills"] = additional_skills

    # Publish the exact isolation context used by this launch.
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

    # Build resume metadata without seeding an automatic session title.
    stage_name = effective_initial_variables.get("stage_name")
    stage_state = effective_initial_variables.get("stage_state")
    resume_metadata = build_resume_metadata(
        provider=spawn_config.provider,
        model=requested_model_selector,
        requested_reasoning_effort=reasoning.requested_effort,
        effective_reasoning_effort=reasoning.effective_effort,
        reasoning_required=reasoning.reasoning_required,
        reasoning_status=reasoning.status,
        reasoning_message=reasoning.message,
        sandbox_config=effective_sandbox_config,
        cwd=str(isolation_ctx.cwd),
        project_id=spawn_config.project_id,
        project_path=spawn_config.project_path,
        parent_session_id=spawn_config.parent_session_id,
        isolation=effective_isolation,
        worktree_id=isolation_ctx.worktree_id,
        clone_id=isolation_ctx.clone_id,
        branch_name=isolation_ctx.branch_name,
        base_branch=spawn_config.base_branch,
        base_commit_sha=base_commit_sha if isinstance(base_commit_sha, str) else None,
        task_id=spawn_config.task_id,
        task_ref=f"#{spawn_config.task_seq_num}"
        if spawn_config.task_seq_num
        else spawn_config.task_id,
        stage_name=stage_name if isinstance(stage_name, str) else None,
        stage_state=stage_state if isinstance(stage_state, str) else None,
        agent_slug=agent_display_name,
        workflow=effective_workflow,
        initial_variables=effective_initial_variables,
    )
    resume_metadata.update(context_updates)
    return effective_initial_variables, resume_metadata


class SpawnRunStorage(Protocol):
    def update_child_session(self, run_id: str, child_session_id: str) -> object: ...

    def update_runtime(
        self,
        run_id: str,
        *,
        pid: int | None = None,
        terminal_id: str | None = None,
        worktree_id: str | None = None,
        clone_id: str | None = None,
    ) -> None: ...


class SpawnRuntimeRunner(Protocol):
    @property
    def run_storage(self) -> SpawnRunStorage: ...


def _parent_session_ref(session_manager: Any | None, parent_session_id: str) -> str:
    """Return the coordinator's canonical reference."""
    if session_manager is None:
        return parent_session_id
    try:
        parent_session = session_manager.get(parent_session_id)
    except Exception:
        logger.debug("Failed to load parent session %s", parent_session_id, exc_info=True)
        return parent_session_id
    return parent_session.ref if parent_session is not None else parent_session_id


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | dict):
        return []
    return [item for item in value if isinstance(item, str)]


def _normalize_optional_model(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value if value and value.lower() != "inherit" else None


def _persist_spawn_runtime(
    runner: SpawnRuntimeRunner,
    run_id: str,
    spawn_result: Any,
    *,
    worktree_id: str | None,
    clone_id: str | None,
    terminal_id: str | None = None,
) -> None:
    child_session_id = getattr(spawn_result, "child_session_id", None)
    if child_session_id is not None:
        try:
            runner.run_storage.update_child_session(run_id, child_session_id)
        except Exception as e:
            logger.warning("Failed to update child_session_id for %s: %s", run_id, e)

    try:
        runner.run_storage.update_runtime(
            run_id,
            pid=getattr(spawn_result, "pid", None),
            terminal_id=terminal_id or getattr(spawn_result, "terminal_id", None),
            worktree_id=worktree_id,
            clone_id=clone_id,
        )
    except Exception as e:
        logger.warning("Failed to persist runtime state for %s: %s", run_id, e)
