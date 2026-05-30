"""Resume cancelled daemon-stop agent runs from persisted launch metadata."""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import psycopg

from gobby.agents.spawn import prepare_terminal_spawn
from gobby.agents.spawn_cache_policy import SPAWN_CACHE_ENV_VARS
from gobby.agents.spawners.command_builder import build_cli_command
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.agents.trust import pre_approve_directory
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_actionable

logger = logging.getLogger(__name__)

SUPPORTED_RESUME_PROVIDERS = frozenset({"claude", "gemini", "qwen", "grok", "codex", "droid"})
DAEMON_STOP_CONTINUATION_PROMPT = (
    "Continue the interrupted task after the Gobby daemon stopped. Inspect the current "
    "workspace state, preserve any existing work, and continue from where the prior run left off."
)
_RESUME_METADATA_ENV_KEYS = frozenset(SPAWN_CACHE_ENV_VARS)


class _RunStorage(Protocol):
    def update_resume_metadata(self, run_id: str, metadata: dict[str, Any]) -> Any: ...

    def update_child_session(self, run_id: str, child_session_id: str) -> Any: ...

    def update_runtime(
        self,
        run_id: str,
        *,
        pid: int | None,
        tmux_session_name: str | None,
        worktree_id: str | None,
        clone_id: str | None,
    ) -> Any: ...

    def start(self, run_id: str) -> Any: ...

    def fail(self, run_id: str, *, error: str) -> Any: ...


class _ResumeRunner(Protocol):
    child_session_manager: Any
    run_storage: _RunStorage


class _SessionLookup(Protocol):
    def get(self, session_id: str) -> Any: ...


@dataclass(frozen=True)
class ResumeAgentResult:
    success: bool
    run_id: str | None = None
    child_session_id: str | None = None
    error: str | None = None


async def resume_agent_run(
    original_run: AgentRun,
    *,
    resume_metadata: dict[str, Any],
    runner: _ResumeRunner,
    session_manager: _SessionLookup,
    task_manager: Any | None = None,
    daemon_config: Any | None = None,
) -> ResumeAgentResult:
    """Start a provider-native resume process for a daemon-stop run.

    Args:
        original_run: Cancelled daemon-stop agent run being resumed.
        resume_metadata: Persisted launch snapshot from the original run.
        runner: Agent runner exposing child session creation and run storage.
        session_manager: Session lookup used to recover provider-native IDs.
        task_manager: Optional task manager used to claim the resumed child session.
        daemon_config: Optional daemon config used for tmux spawn settings.
    """
    provider = _metadata_str(resume_metadata, "provider") or original_run.provider
    if provider not in SUPPORTED_RESUME_PROVIDERS:
        return ResumeAgentResult(False, error=f"resume_unsupported_provider:{provider}")
    if provider == "droid" and shutil.which("droid") is None:
        return ResumeAgentResult(False, error="droid CLI not found in PATH")

    native_session_id = _provider_native_session_id(
        original_run,
        resume_metadata,
        session_manager=session_manager,
        provider=provider,
    )
    if native_session_id is None:
        return ResumeAgentResult(False, error="provider_native_session_id_missing")

    cwd = _metadata_str(resume_metadata, "cwd") or _metadata_str(resume_metadata, "workspace_path")
    project_id = _metadata_str(resume_metadata, "project_id")
    parent_session_id = _metadata_str(resume_metadata, "parent_session_id")
    if not cwd or not project_id or not parent_session_id:
        return ResumeAgentResult(False, error="resume_metadata_incomplete")
    cwd_path = Path(cwd).expanduser()
    if not cwd_path.is_absolute():
        return ResumeAgentResult(False, error="resume_cwd_not_absolute")
    cwd = str(cwd_path)

    prompt = original_run.continuation_prompt or _metadata_str(
        resume_metadata, "continuation_prompt"
    )
    if not prompt:
        prompt = DAEMON_STOP_CONTINUATION_PROMPT

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    metadata = dict(resume_metadata)
    metadata["resumed_from_run_id"] = original_run.id
    metadata["provider_native_session_id"] = native_session_id

    initial_variables = dict(resume_metadata.get("initial_variables") or {})
    initial_variables["daemon_stop_resume"] = True
    initial_variables["resumed_from_agent_run_id"] = original_run.id

    spawn_context = prepare_terminal_spawn(
        session_manager=runner.child_session_manager,
        parent_session_id=parent_session_id,
        project_id=project_id,
        machine_id=_metadata_str(resume_metadata, "machine_id") or "unknown",
        source=provider,
        workflow_name=_metadata_str(resume_metadata, "workflow"),
        agent_name=_metadata_str(resume_metadata, "agent_slug") or original_run.agent_name,
        initial_variables=initial_variables,
        title=_resume_title(original_run),
        git_branch=_metadata_str(resume_metadata, "branch_name"),
        prompt=prompt,
        model=_metadata_str(resume_metadata, "model"),
        is_local=bool(getattr(original_run, "is_local", False)),
        agent_run_id=run_id,
        task_id=original_run.task_id,
        claimed_session_id=original_run.claimed_session_id,
        timeout_seconds=original_run.timeout_seconds,
        sandbox_enabled=_sandbox_enabled(resume_metadata),
        requested_reasoning_effort=_metadata_str(resume_metadata, "requested_reasoning_effort"),
        effective_reasoning_effort=_metadata_str(resume_metadata, "effective_reasoning_effort"),
        reasoning_required=bool(resume_metadata.get("reasoning_required")),
        reasoning_status=_metadata_str(resume_metadata, "reasoning_status") or "not_requested",
        reasoning_message=_metadata_str(resume_metadata, "reasoning_message"),
        resume_metadata_json=metadata,
    )

    sandbox_args = _str_list(resume_metadata.get("sandbox_args"))
    command, _cmd_env = build_cli_command(
        cli=provider,
        prompt=None if provider == "claude" else prompt,
        resume_session_id=native_session_id,
        auto_approve=bool(resume_metadata.get("auto_approve", True)),
        working_directory=cwd if provider in {"codex", "droid", "grok"} else None,
        sandbox_args=None if provider == "claude" else sandbox_args,
        model=_metadata_str(resume_metadata, "model"),
        reasoning_effort=_metadata_str(resume_metadata, "effective_reasoning_effort"),
        config_overrides=_str_list(resume_metadata.get("config_overrides")),
    )
    if provider == "claude":
        claude_mcp_config = Path(cwd) / ".mcp.json"
        if claude_mcp_config.exists():
            command.extend(["--mcp-config", str(claude_mcp_config), "--strict-mcp-config"])
        command.extend(sandbox_args)
        command.append(prompt)

    env = _safe_resume_metadata_env(resume_metadata.get("env"))
    env.update(spawn_context.env_vars)
    env.update(_str_dict(resume_metadata.get("sandbox_env")))
    env["GOBBY_MACHINE_ID"] = _metadata_str(resume_metadata, "machine_id") or "unknown"
    metadata["env"] = _safe_resume_metadata_env(env)
    try:
        update_resume_metadata = getattr(runner.run_storage, "update_resume_metadata", None)
        if callable(update_resume_metadata):
            update_resume_metadata(run_id, metadata)
    except Exception:
        logger.warning("Failed to persist resumed launch metadata for %s", run_id, exc_info=True)

    pre_approve_directory(provider, cwd)
    terminal_result = _tmux_spawner(daemon_config, resume_metadata).spawn(
        command=command, cwd=cwd, env=env
    )
    if not terminal_result.success:
        error = terminal_result.error or terminal_result.message or "resume_spawn_failed"
        _fail_run(runner, run_id, error)
        return ResumeAgentResult(False, run_id=run_id, error=error)

    _persist_resume_runtime(
        runner,
        run_id,
        child_session_id=spawn_context.session_id,
        pid=terminal_result.pid,
        tmux_session_name=getattr(terminal_result, "tmux_session_name", None),
        worktree_id=_metadata_str(resume_metadata, "worktree_id"),
        clone_id=_metadata_str(resume_metadata, "clone_id"),
    )
    _claim_task_for_resume(task_manager, original_run, spawn_context.session_id)
    _fire_resume_started(original_run, run_id, provider, terminal_result, parent_session_id)
    return ResumeAgentResult(True, run_id=run_id, child_session_id=spawn_context.session_id)


def _provider_native_session_id(
    run: AgentRun,
    metadata: dict[str, Any],
    *,
    session_manager: Any,
    provider: str,
) -> str | None:
    for value in (
        _metadata_str(metadata, "provider_native_session_id"),
        run.sdk_session_id,
    ):
        if value:
            return value
    if not run.child_session_id:
        return None
    try:
        session = session_manager.get(run.child_session_id)
    except LookupError:
        return None
    value = getattr(session, "external_id", None)
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("agent-"):
        return None
    if value == run.child_session_id and provider != "claude":
        return None
    return value


def _persist_resume_runtime(
    runner: Any,
    run_id: str,
    *,
    child_session_id: str,
    pid: int | None,
    tmux_session_name: str | None,
    worktree_id: str | None,
    clone_id: str | None,
) -> None:
    runner.run_storage.update_child_session(run_id, child_session_id)
    runner.run_storage.update_runtime(
        run_id,
        pid=pid,
        tmux_session_name=tmux_session_name,
        worktree_id=worktree_id,
        clone_id=clone_id,
    )
    runner.run_storage.start(run_id)


def _claim_task_for_resume(task_manager: Any | None, run: AgentRun, child_session_id: str) -> None:
    if task_manager is None or not run.task_id:
        return
    try:
        task = task_manager.get_task(run.task_id)
        if not task or not is_task_actionable(task):
            return
        current_owner = get_claimed_session_id(task)
        prior_owners = {None, run.child_session_id, run.claimed_session_id}
        if current_owner in prior_owners:
            task_manager.claim_task(run.task_id, session_id=child_session_id)
    except (ValueError, psycopg.Error) as exc:
        logger.warning(
            "Failed to claim task for resumed agent",
            exc_info=True,
            extra={
                "run_id": run.id,
                "task_id": run.task_id,
                "child_session_id": child_session_id,
                "claimed_session_id": run.claimed_session_id,
                "previous_child_session_id": run.child_session_id,
                "error": str(exc),
            },
        )


def _fire_resume_started(
    original_run: AgentRun,
    run_id: str,
    provider: str,
    terminal_result: Any,
    parent_session_id: str,
) -> None:
    try:
        from gobby.runner_broadcasting import fire_agent_event

        fire_agent_event(
            "agent_started",
            run_id,
            {
                "resumed_from_run_id": original_run.id,
                "parent_session_id": parent_session_id,
                "provider": provider,
                "pid": terminal_result.pid,
                "tmux_session_name": getattr(terminal_result, "tmux_session_name", None),
                "tmux_socket_name": getattr(terminal_result, "tmux_socket_name", None),
                "tmux_socket_path": getattr(terminal_result, "tmux_socket_path", None),
            },
        )
    except Exception as exc:
        logger.warning("Failed to fire resumed agent_started event for %s: %s", run_id, exc)


def _fail_run(runner: Any, run_id: str, error: str) -> None:
    try:
        runner.run_storage.fail(run_id, error=error)
    except Exception as exc:
        logger.warning("Failed to mark resumed agent run %s failed: %s", run_id, exc)


def _safe_resume_metadata_env(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if key in _RESUME_METADATA_ENV_KEYS}


def _tmux_spawner(daemon_config: Any | None, metadata: dict[str, Any]) -> TmuxSpawner:
    stored_config = metadata.get("tmux_config")
    if isinstance(stored_config, dict):
        try:
            return TmuxSpawner(config=TmuxConfig.model_validate(stored_config))
        except Exception as exc:
            logger.warning("Failed to load persisted tmux resume config: %s", exc)
    tmux_config = getattr(daemon_config, "tmux", None)
    return TmuxSpawner(config=tmux_config if isinstance(tmux_config, TmuxConfig) else None)


def _resume_title(run: AgentRun) -> str | None:
    if run.agent_name:
        return f"{run.agent_name}: resumed after daemon stop"
    return "Agent: resumed after daemon stop"


def _metadata_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) and value else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, str)]


def _str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _sandbox_enabled(metadata: dict[str, Any]) -> bool:
    sandbox_config = metadata.get("sandbox_config")
    return isinstance(sandbox_config, dict) and bool(sandbox_config.get("enabled"))
