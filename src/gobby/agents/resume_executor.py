"""Resume cancelled daemon-stop agent runs from persisted launch metadata."""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg

from gobby.agents.capture import CaptureStorage, capture_then_kill_async
from gobby.agents.resume_metadata import merge_resume_metadata_env
from gobby.agents.sandbox import coerce_sandbox_config, get_sandbox_resolver
from gobby.agents.spawn import prepare_terminal_spawn
from gobby.agents.spawners.command_builder import build_cli_command
from gobby.agents.srt_runtime import (
    SandboxLaunch,
    SrtRuntimeError,
    prepare_sandbox_launch,
)
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.agents.trust import pre_approve_directory
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_actionable

logger = logging.getLogger(__name__)

SUPPORTED_RESUME_PROVIDERS = frozenset({"claude", "qwen", "grok", "codex", "droid"})
DAEMON_STOP_CONTINUATION_PROMPT = (
    "Continue the interrupted task after the Gobby daemon stopped. Inspect the current "
    "workspace state, preserve any existing work, and continue from where the prior run left off."
)
RESUME_CONSUMED_AT_KEY = "daemon_stop_resume_consumed_at"
RESUME_CONSUMED_BY_RUN_ID_KEY = "daemon_stop_resume_consumed_by_run_id"


class _ResumePreflightError(RuntimeError):
    """Resume cannot safely acquire the resources needed to spawn."""


class _RunStorage(CaptureStorage, Protocol):
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

    def start(self, run_id: str) -> AgentRun | None: ...


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
    worktree_manager: Any | None = None,
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

    run_id = str(uuid.uuid4())
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
    try:
        _mark_original_run_consumed(runner, original_run, resume_metadata, run_id)
        _claim_task_for_resume(task_manager, original_run, spawn_context.session_id)
        _claim_worktree_for_resume(
            worktree_manager or _worktree_manager_from_runner(runner),
            original_run,
            resume_metadata,
            spawn_context.session_id,
        )
    except _ResumePreflightError as exc:
        error = str(exc)
        _fail_run(runner, run_id, error)
        return ResumeAgentResult(False, run_id=run_id, error=error)
    except (ValueError, psycopg.Error) as exc:
        error = f"resume_preflight_failed:{type(exc).__name__}"
        logger.warning(
            "Failed resume preflight for %s",
            run_id,
            exc_info=True,
            extra={"run_id": run_id, "original_run_id": original_run.id, "error": str(exc)},
        )
        _fail_run(runner, run_id, error)
        return ResumeAgentResult(False, run_id=run_id, error=error)

    env = merge_resume_metadata_env(resume_metadata.get("env"))
    env.update(spawn_context.env_vars)
    env["GOBBY_MACHINE_ID"] = _metadata_str(resume_metadata, "machine_id") or "unknown"
    sandbox_config = coerce_sandbox_config(resume_metadata.get("sandbox_config"))
    launch = SandboxLaunch(backend="provider-native", enforced=False)
    if sandbox_config is not None:
        resolver = None
        if sandbox_config.enabled and sandbox_config.backend == "provider-native":
            try:
                resolver = get_sandbox_resolver(provider)
            except ValueError:
                error = f"resume_sandbox_unsupported:{provider}"
                _fail_run(runner, run_id, error)
                return ResumeAgentResult(False, run_id=run_id, error=error)
        daemon_port = int(getattr(daemon_config, "daemon_port", 60887))
        websocket = getattr(daemon_config, "websocket", None)
        websocket_port = int(getattr(websocket, "port", 60888))
        try:
            launch = await prepare_sandbox_launch(
                config=sandbox_config,
                provider=provider,
                workspace_path=cwd,
                run_id=run_id,
                resolver=resolver,
                daemon_port=daemon_port,
                websocket_port=websocket_port,
                api_base=_resume_api_base(provider, env),
                env=env,
            )
        except (OSError, ValueError, SrtRuntimeError) as exc:
            error = f"resume_sandbox_failed_closed:{type(exc).__name__}:{exc}"
            _fail_run(runner, run_id, error)
            return ResumeAgentResult(False, run_id=run_id, error=error)
    env.update(launch.provider_env)
    update_sandbox_enabled = getattr(runner.child_session_manager, "update_sandbox_enabled", None)
    if callable(update_sandbox_enabled):
        update_sandbox_enabled(spawn_context.session_id, launch.enforced)
    update_policy_hash = getattr(
        runner.child_session_manager,
        "update_sandbox_policy_hash",
        None,
    )
    if launch.policy_hash and callable(update_policy_hash):
        update_policy_hash(spawn_context.session_id, launch.policy_hash)

    sandbox_args = launch.provider_args
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
        claude_mcp_path = _metadata_str(resume_metadata, "mcp_path")
        strict_mcp = bool(resume_metadata.get("strict_mcp"))
        if not claude_mcp_path:
            claude_mcp_config = Path(cwd) / ".mcp.json"
            if claude_mcp_config.exists():
                claude_mcp_path = str(claude_mcp_config)
                strict_mcp = True
                metadata["mcp_path"] = claude_mcp_path
                metadata["strict_mcp"] = strict_mcp
        if claude_mcp_path:
            command.extend(["--mcp-config", claude_mcp_path])
            if strict_mcp:
                command.append("--strict-mcp-config")
        command.extend(sandbox_args)
        command.append(prompt)
    command = launch.wrap(command)
    metadata["env"] = merge_resume_metadata_env(env)
    metadata["sandbox_args"] = list(launch.provider_args)
    metadata["sandbox_env"] = dict(launch.provider_env)
    metadata["sandbox"] = launch.metadata()
    try:
        update_resume_metadata = getattr(runner.run_storage, "update_resume_metadata", None)
        if callable(update_resume_metadata):
            update_resume_metadata(run_id, metadata)
    except Exception:
        logger.warning("Failed to persist resumed launch metadata for %s", run_id, exc_info=True)

    pre_approve_directory(provider, cwd)
    terminal_result = await asyncio.to_thread(
        _tmux_spawner(daemon_config, resume_metadata).spawn,
        command=command,
        cwd=cwd,
        env=env,
    )
    if not terminal_result.success:
        error = terminal_result.error or terminal_result.message or "resume_spawn_failed"
        _fail_run(runner, run_id, error)
        return ResumeAgentResult(False, run_id=run_id, error=error)

    tmux_session_name = getattr(terminal_result, "tmux_session_name", None)
    try:
        started_run = _persist_resume_runtime(
            runner,
            run_id,
            child_session_id=spawn_context.session_id,
            pid=terminal_result.pid,
            tmux_session_name=tmux_session_name,
            worktree_id=_metadata_str(resume_metadata, "worktree_id"),
            clone_id=_metadata_str(resume_metadata, "clone_id"),
        )
    except Exception as exc:
        error = f"resume_runtime_persist_failed:{type(exc).__name__}"
        await _kill_spawned_tmux_session(
            runner.run_storage,
            run_id,
            tmux_session_name,
            reason=error,
        )
        if not tmux_session_name:
            _fail_run(runner, run_id, error)
        return ResumeAgentResult(False, run_id=run_id, error=error)
    if started_run is None:
        await _kill_spawned_tmux_session(
            runner.run_storage,
            run_id,
            tmux_session_name,
            reason="agent_run_start_skipped",
        )
        return ResumeAgentResult(False, run_id=run_id, error="agent_run_start_skipped")
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
    runner: _ResumeRunner,
    run_id: str,
    *,
    child_session_id: str,
    pid: int | None,
    tmux_session_name: str | None,
    worktree_id: str | None,
    clone_id: str | None,
) -> AgentRun | None:
    runner.run_storage.update_child_session(run_id, child_session_id)
    runner.run_storage.update_runtime(
        run_id,
        pid=pid,
        tmux_session_name=tmux_session_name,
        worktree_id=worktree_id,
        clone_id=clone_id,
    )
    return runner.run_storage.start(run_id)


def _mark_original_run_consumed(
    runner: _ResumeRunner,
    original_run: AgentRun,
    resume_metadata: dict[str, Any],
    resumed_run_id: str,
) -> None:
    metadata = dict(original_run.resume_metadata_json or resume_metadata)
    metadata[RESUME_CONSUMED_AT_KEY] = datetime.now(UTC).isoformat()
    metadata[RESUME_CONSUMED_BY_RUN_ID_KEY] = resumed_run_id
    updated = runner.run_storage.update_resume_metadata(original_run.id, metadata)
    if updated is None:
        raise _ResumePreflightError("resume_candidate_consume_failed")


def _claim_task_for_resume(task_manager: Any | None, run: AgentRun, child_session_id: str) -> None:
    if not run.task_id:
        return
    if task_manager is None:
        raise _ResumePreflightError("resume_task_manager_missing")
    task = task_manager.get_task(run.task_id)
    if not task:
        raise _ResumePreflightError("resume_task_missing")
    if not is_task_actionable(task):
        raise _ResumePreflightError("resume_task_not_actionable")
    current_owner = get_claimed_session_id(task)
    prior_owners = {None, run.child_session_id, run.claimed_session_id, child_session_id}
    if current_owner not in prior_owners:
        raise _ResumePreflightError("resume_task_claim_conflict")
    claimed = task_manager.claim_task(run.task_id, session_id=child_session_id)
    if get_claimed_session_id(claimed) != child_session_id:
        raise _ResumePreflightError("resume_task_claim_failed")


def _claim_worktree_for_resume(
    worktree_manager: Any | None,
    run: AgentRun,
    resume_metadata: dict[str, Any],
    child_session_id: str,
) -> None:
    worktree_id = _metadata_str(resume_metadata, "worktree_id")
    if not worktree_id:
        return
    if worktree_manager is None:
        raise _ResumePreflightError("resume_worktree_manager_missing")
    allowed_sessions = {None, run.child_session_id, run.claimed_session_id, child_session_id}
    claim_if_available = getattr(worktree_manager, "claim_if_available", None)
    if callable(claim_if_available):
        claimed = claim_if_available(
            worktree_id,
            child_session_id,
            allowed_existing_session_ids=allowed_sessions,
        )
    else:
        worktree = worktree_manager.get(worktree_id)
        if worktree is None or getattr(worktree, "agent_session_id", None) not in allowed_sessions:
            claimed = None
        else:
            claimed = worktree_manager.claim(worktree_id, child_session_id)
    if claimed is None or getattr(claimed, "agent_session_id", None) != child_session_id:
        raise _ResumePreflightError("resume_worktree_claim_failed")


def _worktree_manager_from_runner(runner: _ResumeRunner) -> Any | None:
    storage = getattr(getattr(runner, "child_session_manager", None), "_storage", None)
    db = getattr(storage, "db", None)
    if db is None:
        return None
    return LocalWorktreeManager(db)


async def _kill_spawned_tmux_session(
    storage: CaptureStorage,
    run_id: str,
    tmux_session_name: str | None,
    *,
    reason: str,
) -> None:
    if not tmux_session_name:
        return
    try:
        from gobby.agents.tmux import get_tmux_session_manager

        run = await asyncio.to_thread(storage.get, run_id)
        if run is None:
            logger.warning(
                "Refusing raw tmux kill for missing resumed run %s",
                run_id,
            )
            return
        tmux = get_tmux_session_manager()
        session_name = str(tmux_session_name)
        result = await capture_then_kill_async(
            storage=storage,
            run_id=run.id,
            session_name=session_name,
            action="fail",
            reason=reason,
            session_alive=lambda: tmux.has_session(session_name),
            capture=lambda: tmux.capture_full_pane(session_name),
            kill=lambda: tmux.kill_session(session_name, missing_ok=True),
        )
        if not result.success:
            raise RuntimeError(f"{result.error_code}: {result.error}")
    except Exception as exc:
        logger.warning(
            "Failed to kill tmux session after resume persistence failure",
            exc_info=True,
            extra={"run_id": run_id, "tmux_session_name": tmux_session_name, "error": str(exc)},
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


def _tmux_spawner(daemon_config: Any | None, metadata: dict[str, Any]) -> TmuxSpawner:
    stored_config = metadata.get("tmux_config")
    if isinstance(stored_config, dict):
        try:
            return TmuxSpawner(config=TmuxConfig.model_validate(stored_config))
        except Exception as exc:
            logger.warning("Failed to load persisted tmux resume config: %s", exc)
    tmux_config = getattr(daemon_config, "tmux", None)
    if not isinstance(tmux_config, TmuxConfig):
        raise RuntimeError("daemon tmux config is required to resume tmux agents")
    return TmuxSpawner(config=tmux_config)


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


def _resume_api_base(provider: str, env: dict[str, str]) -> str | None:
    key = {
        "claude": "ANTHROPIC_BASE_URL",
        "codex": "OPENAI_BASE_URL",
        "droid": "FACTORY_API_BASE_URL",
        "grok": "GROK_API_BASE",
        "qwen": "QWEN_API_BASE",
    }.get(provider)
    return env.get(key) if key else None


def _sandbox_enabled(metadata: dict[str, Any]) -> bool:
    sandbox_config = metadata.get("sandbox_config")
    return isinstance(sandbox_config, dict) and bool(sandbox_config.get("enabled"))
