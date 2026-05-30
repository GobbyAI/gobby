"""
Unified Spawn Executor for Agent Spawning.

This module consolidates the spawn dispatch logic from agents.py, worktrees.py,
and clones.py into a single executor. All agents spawn via tmux.
"""

import json
import logging
import shutil
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from gobby.agents.constants import ALL_TERMINAL_ENV_VARS
from gobby.agents.sandbox import (
    CodexSandboxResolver,
    GeminiSandboxResolver,
    GrokSandboxResolver,
    QwenSandboxResolver,
    compute_sandbox_paths,
)
from gobby.agents.spawn_cache_policy import (
    PATH_ENV_VAR,
    SPAWN_CACHE_ENV_VARS,
    merge_spawn_path_env,
)
from gobby.agents.spawn_cache_policy import (
    sandbox_config_for_spawn as _sandbox_config_for_spawn,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.trust import pre_approve_directory
from gobby.providers import AGY_UNAVAILABLE_REASON

if TYPE_CHECKING:
    from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn import (
    build_cli_command,
    prepare_terminal_spawn,
)
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.config.tmux import TmuxConfig

logger = logging.getLogger(__name__)
_RESERVED_EXTRA_ENV_KEYS = frozenset(
    (*ALL_TERMINAL_ENV_VARS, *SPAWN_CACHE_ENV_VARS, "GOBBY_MACHINE_ID")
)

# Spawned Codex agents must be able to use Gobby's progressive-discovery MCP flow without
# interactive approval loops. Keep this allowlist narrow: these tools expose discovery,
# schema lookup, dispatch, and session variables, not arbitrary filesystem or shell access.
_CODEX_PREAPPROVED_GOBBY_TOOLS = [
    "list_mcp_servers",
    "list_tools",
    "get_tool_schema",
    "call_tool",
    "get_variable",
    "set_variable",
]


def _tmux_spawner_for_request(request: SpawnRequest) -> TmuxSpawner:
    daemon_config = request.daemon_config
    tmux_config = getattr(daemon_config, "tmux", None)

    return TmuxSpawner(config=tmux_config if isinstance(tmux_config, TmuxConfig) else None)


def _apply_extra_env(env: dict[str, str], request: SpawnRequest) -> None:
    if request.extra_env:
        for key, value in request.extra_env.items():
            if key == PATH_ENV_VAR:
                merge_spawn_path_env(env, value)
                continue
            if key in _RESERVED_EXTRA_ENV_KEYS:
                logger.warning("Ignoring reserved spawn environment override for %s", key)
                continue
            env[key] = value


def _record_resume_launch_details(
    request: SpawnRequest,
    *,
    agent_run_id: str,
    sandbox_args: list[str] | None = None,
    sandbox_env: dict[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    config_overrides: list[str] | None = None,
) -> None:
    """Persist post-resolution CLI launch details for daemon-stop resume."""
    if request.resume_metadata_json is None:
        return
    storage = getattr(request.session_manager, "_storage", None)
    db = getattr(storage, "db", None)
    if db is None:
        return
    metadata = dict(request.resume_metadata_json)
    metadata["sandbox_args"] = list(sandbox_args or [])
    metadata["sandbox_env"] = dict(sandbox_env or {})
    final_env = {
        str(key): str(value)
        for source in (
            metadata.get("env") if isinstance(metadata.get("env"), Mapping) else None,
            request.extra_env,
            env,
        )
        if source is not None
        for key, value in source.items()
    }
    metadata["env"] = final_env
    metadata["config_overrides"] = list(config_overrides or [])
    tmux_config = getattr(request.daemon_config, "tmux", None)
    if isinstance(tmux_config, TmuxConfig):
        metadata["tmux_config"] = tmux_config.model_dump()
    try:
        from gobby.storage.agents import LocalAgentRunManager

        LocalAgentRunManager(db).update_resume_metadata(agent_run_id, metadata)
    except Exception as exc:
        logger.warning("Failed to persist resume launch metadata: %s", exc)


def _session_manager_validation_error(
    request: SpawnRequest,
    provider_name: str,
) -> SpawnResult | None:
    manager = request.session_manager
    if manager is None:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=None,
            status="failed",
            error=f"session_manager is required for {provider_name} spawn",
        )

    has_storage_db = getattr(getattr(manager, "_storage", None), "db", None) is not None
    required_methods = ("create_child_session", "update_terminal_pickup_metadata")
    missing_methods = [
        method for method in required_methods if not callable(getattr(manager, method, None))
    ]
    if has_storage_db and not missing_methods:
        return None

    if not has_storage_db:
        detail = "session_manager._storage.db is missing"
    else:
        detail = f"session_manager is missing methods: {', '.join(missing_methods)}"

    return SpawnResult(
        success=False,
        run_id=request.run_id,
        child_session_id=None,
        status="failed",
        error=f"invalid session_manager for {provider_name} spawn — {detail}",
    )


async def execute_spawn(request: SpawnRequest) -> SpawnResult:
    """
    Unified spawn dispatch — all agents spawn via tmux.

    Routes to provider-specific terminal spawners based on request.provider.

    Args:
        request: SpawnRequest with all spawn parameters

    Returns:
        SpawnResult with spawn outcome and metadata
    """
    if request.provider == "gemini":
        return await _spawn_gemini_terminal(request)
    elif request.provider == "grok":
        return await _spawn_grok_terminal(request)
    elif request.provider == "qwen":
        return await _spawn_qwen_terminal(request)
    elif request.provider == "codex":
        return await _spawn_codex_terminal(request)
    elif request.provider == "droid":
        return await _spawn_droid_terminal(request)
    elif request.provider == "agy":
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=None,
            status="failed",
            error=AGY_UNAVAILABLE_REASON,
        )
    # Unknown providers intentionally preserve the historical Claude fallback.
    return await _spawn_claude_terminal(request)


async def _spawn_claude_terminal(request: SpawnRequest) -> SpawnResult:
    """
    Spawn Claude agent in terminal with proper session/workflow setup.

    Uses prepare_terminal_spawn to:
    1. Create child session with parent linkage
    2. Pass initial_variables for workflow activation (e.g., assigned_task_id)
    3. Set up environment variables for session matching
    """
    if validation_error := _session_manager_validation_error(request, "Claude"):
        return validation_error

    # Prepare spawn context (creates child session, builds env vars)
    spawn_context = prepare_terminal_spawn(
        session_manager=cast("ChildSessionManager", request.session_manager),
        parent_session_id=request.parent_session_id,
        project_id=request.project_id,
        machine_id=request.machine_id or "unknown",
        source="claude",
        workflow_name=request.workflow,
        initial_variables=request.initial_variables,
        prompt=request.prompt,
        max_agent_depth=request.max_agent_depth,
        git_branch=request.branch_name,
        agent_run_id=request.agent_run_id,
        task_id=request.task_id,
        claimed_session_id=request.claimed_session_id,
        title=request.title,
        agent_name=request.agent_name,
        model=request.model,
        is_local=request.is_local,
        timeout_seconds=request.timeout_seconds,
        sandbox_enabled=bool(request.sandbox_config and request.sandbox_config.enabled),
        requested_reasoning_effort=request.requested_reasoning_effort,
        effective_reasoning_effort=request.effective_reasoning_effort,
        reasoning_required=request.reasoning_required,
        reasoning_status=request.reasoning_status,
        reasoning_message=request.reasoning_message,
        resume_metadata_json=request.resume_metadata_json,
    )

    gobby_session_id = spawn_context.session_id

    # Build command for Claude CLI
    # Pass session_id so Claude uses --session-id flag, which allows the
    # SessionStart hook to match this process to the pre-created session
    # (and auto-activate the workflow, which delivers the prompt via on_enter).
    cmd, _cmd_env = build_cli_command(
        cli="claude",
        prompt=request.prompt,
        session_id=gobby_session_id,
        auto_approve=True,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
    )

    # Resolve sandbox config if provided
    sandbox_config = _sandbox_config_for_spawn(request.sandbox_config, spawn_context.env_vars)
    sandbox_args: list[str] = []
    sandbox_env: dict[str, str] = {}
    if sandbox_config and sandbox_config.enabled:
        # Claude uses its own sandbox resolver
        from gobby.agents.sandbox import ClaudeSandboxResolver

        resolver = ClaudeSandboxResolver()
        paths = compute_sandbox_paths(
            config=sandbox_config,
            workspace_path=request.cwd,
        )
        sandbox_args, sandbox_env = resolver.resolve(sandbox_config, paths)
        cmd.extend(sandbox_args)

    # Merge env vars: spawn context + sandbox
    env = spawn_context.env_vars.copy()
    if sandbox_env:
        env.update(sandbox_env)
    _apply_extra_env(env, request)

    # Map api_base/api_token to Claude-specific env vars
    if request.api_base:
        env["ANTHROPIC_BASE_URL"] = request.api_base
    if request.api_token:
        env["ANTHROPIC_AUTH_TOKEN"] = request.api_token

    # Pass machine_id as env var for sandboxed agents
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=sandbox_args,
        sandbox_env=sandbox_env,
        env=env,
    )

    # Pre-approve workspace trust so the CLI doesn't show an interactive prompt
    pre_approve_directory("claude", request.cwd)

    # Spawn in terminal with env vars
    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = terminal_spawner.spawn(
        command=cmd,
        cwd=request.cwd,
        env=env,
    )

    if not terminal_result.success:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=gobby_session_id,
            status="failed",
            error=terminal_result.error or terminal_result.message,
        )

    return SpawnResult(
        success=True,
        run_id=spawn_context.agent_run_id,
        child_session_id=gobby_session_id,
        status="pending",
        pid=terminal_result.pid,
        terminal_type=terminal_result.terminal_type,
        tmux_session_name=terminal_result.tmux_session_name,
        tmux_socket_name=terminal_result.tmux_socket_name,
        tmux_socket_path=terminal_result.tmux_socket_path,
        message=f"Claude agent spawned in {terminal_result.terminal_type} with session {gobby_session_id}",
    )


async def _spawn_gemini_terminal(request: SpawnRequest) -> SpawnResult:
    """
    Spawn Gemini agent in terminal with direct spawn (no preflight).

    Session linkage approach:
    1. Pre-create Gobby session with parent linkage (no external_id yet)
    2. Pass GOBBY_SESSION_ID and other env vars to the terminal
    3. Gemini's hook dispatcher reads env vars and includes in SessionStart
    4. Daemon updates external_id when SessionStart fires with Gemini's native session_id

    This avoids the preflight+resume approach which failed because Gemini
    doesn't persist sessions when terminated.
    """
    if validation_error := _session_manager_validation_error(request, "Gemini"):
        return validation_error

    # Prepare spawn context (creates child session, builds env vars)
    spawn_context = prepare_terminal_spawn(
        session_manager=cast("ChildSessionManager", request.session_manager),
        parent_session_id=request.parent_session_id,
        project_id=request.project_id,
        machine_id=request.machine_id or "unknown",
        source="gemini",
        workflow_name=request.workflow,
        initial_variables=request.initial_variables,
        prompt=request.prompt,
        max_agent_depth=request.max_agent_depth,
        git_branch=request.branch_name,
        agent_run_id=request.agent_run_id,
        task_id=request.task_id,
        claimed_session_id=request.claimed_session_id,
        title=request.title,
        agent_name=request.agent_name,
        model=request.model,
        is_local=request.is_local,
        timeout_seconds=request.timeout_seconds,
        sandbox_enabled=bool(request.sandbox_config and request.sandbox_config.enabled),
        requested_reasoning_effort=request.requested_reasoning_effort,
        effective_reasoning_effort=request.effective_reasoning_effort,
        reasoning_required=request.reasoning_required,
        reasoning_status=request.reasoning_status,
        reasoning_message=request.reasoning_message,
        resume_metadata_json=request.resume_metadata_json,
    )

    gobby_session_id = spawn_context.session_id

    sandbox_config = _sandbox_config_for_spawn(request.sandbox_config, spawn_context.env_vars)
    sandbox_args: list[str] = []
    sandbox_env: dict[str, str] = {}
    if sandbox_config and sandbox_config.enabled:
        resolver = GeminiSandboxResolver()
        paths = compute_sandbox_paths(
            config=sandbox_config,
            workspace_path=request.cwd,
        )
        sandbox_args, sandbox_env = resolver.resolve(sandbox_config, paths)

    # Build command for fresh Gemini session (not resume)
    # Session context is injected via additionalContext at SessionStart by the daemon.
    cmd, _cmd_env = build_cli_command(
        cli="gemini",
        prompt=request.prompt,
        auto_approve=True,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=sandbox_args or None,
    )

    # Merge env vars: spawn context + sandbox
    env = spawn_context.env_vars.copy()
    if sandbox_env:
        env.update(sandbox_env)
    _apply_extra_env(env, request)

    # Map api_base/api_token to Gemini-specific env vars
    if request.api_base:
        env["GEMINI_API_BASE"] = request.api_base
    if request.api_token:
        env["GEMINI_API_KEY"] = request.api_token

    # Pass machine_id as env var for sandboxed agents that can't read ~/.gobby/machine_id
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=sandbox_args,
        sandbox_env=sandbox_env,
        env=env,
    )

    # Pre-approve workspace trust so the CLI doesn't show an interactive prompt
    pre_approve_directory("gemini", request.cwd)

    # Spawn in terminal with env vars
    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = terminal_spawner.spawn(
        command=cmd,
        cwd=request.cwd,
        env=env,
    )

    if not terminal_result.success:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=gobby_session_id,
            status="failed",
            error=terminal_result.error or terminal_result.message,
        )

    return SpawnResult(
        success=True,
        run_id=spawn_context.agent_run_id,
        child_session_id=gobby_session_id,
        status="pending",
        pid=terminal_result.pid,
        terminal_type=terminal_result.terminal_type,
        tmux_session_name=terminal_result.tmux_session_name,
        tmux_socket_name=terminal_result.tmux_socket_name,
        tmux_socket_path=terminal_result.tmux_socket_path,
        message=f"Gemini agent spawned in terminal with session {gobby_session_id}",
    )


async def _spawn_qwen_terminal(request: SpawnRequest) -> SpawnResult:
    """
    Spawn Qwen agent in terminal with direct spawn (no preflight).

    Session linkage approach:
    1. Pre-create Gobby session with parent linkage (no external_id yet)
    2. Pass GOBBY_SESSION_ID and other env vars to the terminal
    3. Qwen's hook dispatcher reads env vars and includes in SessionStart
    4. Daemon updates external_id when SessionStart fires with Qwen's native session_id

    This avoids the preflight+resume approach which failed because Qwen
    doesn't persist sessions when terminated.
    """
    if validation_error := _session_manager_validation_error(request, "Qwen"):
        return validation_error

    spawn_context = prepare_terminal_spawn(
        session_manager=cast("ChildSessionManager", request.session_manager),
        parent_session_id=request.parent_session_id,
        project_id=request.project_id,
        machine_id=request.machine_id or "unknown",
        source="qwen",
        workflow_name=request.workflow,
        initial_variables=request.initial_variables,
        prompt=request.prompt,
        max_agent_depth=request.max_agent_depth,
        git_branch=request.branch_name,
        agent_run_id=request.agent_run_id,
        task_id=request.task_id,
        claimed_session_id=request.claimed_session_id,
        title=request.title,
        agent_name=request.agent_name,
        model=request.model,
        is_local=request.is_local,
        timeout_seconds=request.timeout_seconds,
        sandbox_enabled=bool(request.sandbox_config and request.sandbox_config.enabled),
        requested_reasoning_effort=request.requested_reasoning_effort,
        effective_reasoning_effort=request.effective_reasoning_effort,
        reasoning_required=request.reasoning_required,
        reasoning_status=request.reasoning_status,
        reasoning_message=request.reasoning_message,
        resume_metadata_json=request.resume_metadata_json,
    )

    gobby_session_id = spawn_context.session_id

    sandbox_config = _sandbox_config_for_spawn(request.sandbox_config, spawn_context.env_vars)
    sandbox_args: list[str] = []
    sandbox_env: dict[str, str] = {}
    if sandbox_config and sandbox_config.enabled:
        resolver = QwenSandboxResolver()
        paths = compute_sandbox_paths(
            config=sandbox_config,
            workspace_path=request.cwd,
        )
        sandbox_args, sandbox_env = resolver.resolve(sandbox_config, paths)

    cmd, _cmd_env = build_cli_command(
        cli="qwen",
        prompt=request.prompt,
        auto_approve=True,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=sandbox_args or None,
    )

    env = spawn_context.env_vars.copy()
    if sandbox_env:
        env.update(sandbox_env)
    _apply_extra_env(env, request)

    if request.api_base:
        env["QWEN_API_BASE"] = request.api_base
    if request.api_token:
        env["QWEN_API_KEY"] = request.api_token

    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=sandbox_args,
        sandbox_env=sandbox_env,
        env=env,
    )

    pre_approve_directory("qwen", request.cwd)

    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = terminal_spawner.spawn(
        command=cmd,
        cwd=request.cwd,
        env=env,
    )

    if not terminal_result.success:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=gobby_session_id,
            status="failed",
            error=terminal_result.error or terminal_result.message,
        )

    return SpawnResult(
        success=True,
        run_id=spawn_context.agent_run_id,
        child_session_id=gobby_session_id,
        status="pending",
        pid=terminal_result.pid,
        terminal_type=terminal_result.terminal_type,
        tmux_session_name=terminal_result.tmux_session_name,
        tmux_socket_name=terminal_result.tmux_socket_name,
        tmux_socket_path=terminal_result.tmux_socket_path,
        message=f"Qwen agent spawned in terminal with session {gobby_session_id}",
    )


async def _spawn_grok_terminal(request: SpawnRequest) -> SpawnResult:
    """Spawn Grok agent in terminal with direct hook/env-based session linkage."""
    if validation_error := _session_manager_validation_error(request, "Grok"):
        return validation_error

    spawn_context = prepare_terminal_spawn(
        session_manager=cast("ChildSessionManager", request.session_manager),
        parent_session_id=request.parent_session_id,
        project_id=request.project_id,
        machine_id=request.machine_id or "unknown",
        source="grok",
        workflow_name=request.workflow,
        initial_variables=request.initial_variables,
        prompt=request.prompt,
        max_agent_depth=request.max_agent_depth,
        git_branch=request.branch_name,
        agent_run_id=request.agent_run_id,
        task_id=request.task_id,
        claimed_session_id=request.claimed_session_id,
        title=request.title,
        agent_name=request.agent_name,
        model=request.model,
        is_local=request.is_local,
        timeout_seconds=request.timeout_seconds,
        sandbox_enabled=bool(request.sandbox_config and request.sandbox_config.enabled),
        requested_reasoning_effort=request.requested_reasoning_effort,
        effective_reasoning_effort=request.effective_reasoning_effort,
        reasoning_required=request.reasoning_required,
        reasoning_status=request.reasoning_status,
        reasoning_message=request.reasoning_message,
        resume_metadata_json=request.resume_metadata_json,
    )

    gobby_session_id = spawn_context.session_id
    sandbox_config = _sandbox_config_for_spawn(request.sandbox_config, spawn_context.env_vars)
    sandbox_args: list[str] = []
    sandbox_env: dict[str, str] = {}
    if sandbox_config and sandbox_config.enabled:
        resolver = GrokSandboxResolver()
        paths = compute_sandbox_paths(
            config=sandbox_config,
            workspace_path=request.cwd,
        )
        sandbox_args, sandbox_env = resolver.resolve(sandbox_config, paths)

    cmd, _cmd_env = build_cli_command(
        cli="grok",
        prompt=request.prompt,
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=sandbox_args or None,
    )

    env = spawn_context.env_vars.copy()
    if sandbox_env:
        env.update(sandbox_env)
    _apply_extra_env(env, request)

    if request.api_base:
        env["GROK_API_BASE"] = request.api_base
    if request.api_token:
        env["XAI_API_KEY"] = request.api_token
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=sandbox_args,
        sandbox_env=sandbox_env,
        env=env,
    )

    pre_approve_directory("grok", request.cwd)

    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = terminal_spawner.spawn(
        command=cmd,
        cwd=request.cwd,
        env=env,
    )

    if not terminal_result.success:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=gobby_session_id,
            status="failed",
            error=terminal_result.error or terminal_result.message,
        )

    return SpawnResult(
        success=True,
        run_id=spawn_context.agent_run_id,
        child_session_id=gobby_session_id,
        status="pending",
        pid=terminal_result.pid,
        terminal_type=terminal_result.terminal_type,
        tmux_session_name=terminal_result.tmux_session_name,
        tmux_socket_name=terminal_result.tmux_socket_name,
        tmux_socket_path=terminal_result.tmux_socket_path,
        message=f"Grok agent spawned in terminal with session {gobby_session_id}",
    )


async def _spawn_codex_terminal(request: SpawnRequest) -> SpawnResult:
    """
    Spawn Codex agent in terminal with direct spawn (no preflight).

    Session linkage approach (matches Gemini/Qwen):
    1. Pre-create Gobby child session with parent linkage (no external_id yet).
    2. Pass GOBBY_SESSION_ID and other env vars to the terminal.
    3. Codex's hooks.json dispatcher reads env vars and includes them in SessionStart.
    4. Daemon updates external_id when SessionStart fires with Codex's native session_id.

    Replaces the prior `codex exec "exit"` preflight workaround. Codex hooks ship
    via `gobby install --codex`; the SessionStart hook is now the source of truth
    for the native session id.
    """
    if validation_error := _session_manager_validation_error(request, "Codex"):
        return validation_error

    spawn_context = prepare_terminal_spawn(
        session_manager=cast("ChildSessionManager", request.session_manager),
        parent_session_id=request.parent_session_id,
        project_id=request.project_id,
        machine_id=request.machine_id or "unknown",
        source="codex",
        workflow_name=request.workflow,
        initial_variables=request.initial_variables,
        prompt=request.prompt,
        max_agent_depth=request.max_agent_depth,
        git_branch=request.branch_name,
        agent_run_id=request.agent_run_id,
        task_id=request.task_id,
        claimed_session_id=request.claimed_session_id,
        title=request.title,
        agent_name=request.agent_name,
        model=request.model,
        is_local=request.is_local,
        timeout_seconds=request.timeout_seconds,
        sandbox_enabled=bool(request.sandbox_config and request.sandbox_config.enabled),
        requested_reasoning_effort=request.requested_reasoning_effort,
        effective_reasoning_effort=request.effective_reasoning_effort,
        reasoning_required=request.reasoning_required,
        reasoning_status=request.reasoning_status,
        reasoning_message=request.reasoning_message,
        resume_metadata_json=request.resume_metadata_json,
    )

    gobby_session_id = spawn_context.session_id

    sandbox_config = _sandbox_config_for_spawn(request.sandbox_config, spawn_context.env_vars)
    sandbox_args: list[str] = []
    if sandbox_config and sandbox_config.enabled:
        resolver = CodexSandboxResolver()
        paths = compute_sandbox_paths(
            config=sandbox_config,
            workspace_path=request.cwd,
        )
        sandbox_args, _ = resolver.resolve(sandbox_config, paths)

    config_overrides = _codex_mcp_config_overrides(request.project_path)
    cmd, _cmd_env = build_cli_command(
        cli="codex",
        prompt=request.prompt or "",
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=sandbox_args or None,
        config_overrides=config_overrides,
    )

    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)

    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=sandbox_args,
        env=env,
        config_overrides=config_overrides,
    )

    pre_approve_directory("codex", request.cwd)

    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = terminal_spawner.spawn(
        command=cmd,
        cwd=request.cwd,
        env=env,
    )

    if not terminal_result.success:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=gobby_session_id,
            status="failed",
            error=terminal_result.error or terminal_result.message,
        )

    return SpawnResult(
        success=True,
        run_id=spawn_context.agent_run_id,
        child_session_id=gobby_session_id,
        status="pending",
        pid=terminal_result.pid,
        terminal_type=terminal_result.terminal_type,
        tmux_session_name=terminal_result.tmux_session_name,
        tmux_socket_name=terminal_result.tmux_socket_name,
        tmux_socket_path=terminal_result.tmux_socket_path,
        message=f"Codex agent spawned in terminal with session {gobby_session_id}",
    )


def _codex_mcp_config_overrides(project_path: str | None) -> list[str]:
    """Force Codex spawned in isolated workspaces to use the main repo MCP server."""
    if not project_path:
        return []
    args = ["run", "--project", project_path, "gobby", "mcp-server"]
    args_toml = "[" + ",".join(json.dumps(arg) for arg in args) + "]"
    overrides = [
        'mcp_servers.gobby.command="uv"',
        f"mcp_servers.gobby.args={args_toml}",
        "mcp_servers.gobby.startup_timeout_sec=120",
    ]
    # Dotted -c overrides replace enough of the spawned server table that Codex
    # no longer sees user-level per-tool approvals. Re-seed only the Gobby proxy
    # tools required by worker contracts so unattended builds do not stop on MCP
    # permission prompts.
    for tool_name in _CODEX_PREAPPROVED_GOBBY_TOOLS:
        overrides.append(f'mcp_servers.gobby.tools.{tool_name}.approval_mode="approve"')
    return overrides


async def _spawn_droid_terminal(request: SpawnRequest) -> SpawnResult:
    """Spawn Droid agent in terminal with direct hook/env-based session linkage."""
    if validation_error := _session_manager_validation_error(request, "Droid"):
        return validation_error
    if shutil.which("droid") is None:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=None,
            status="failed",
            error="droid CLI not found in PATH. Install droid first: see docs/cli-integrations/droid.md",
        )

    spawn_context = prepare_terminal_spawn(
        session_manager=cast("ChildSessionManager", request.session_manager),
        parent_session_id=request.parent_session_id,
        project_id=request.project_id,
        machine_id=request.machine_id or "unknown",
        source="droid",
        workflow_name=request.workflow,
        initial_variables=request.initial_variables,
        prompt=request.prompt,
        max_agent_depth=request.max_agent_depth,
        git_branch=request.branch_name,
        agent_run_id=request.agent_run_id,
        task_id=request.task_id,
        claimed_session_id=request.claimed_session_id,
        title=request.title,
        agent_name=request.agent_name,
        model=request.model,
        is_local=request.is_local,
        timeout_seconds=request.timeout_seconds,
        sandbox_enabled=bool(request.sandbox_config and request.sandbox_config.enabled),
        requested_reasoning_effort=request.requested_reasoning_effort,
        effective_reasoning_effort=request.effective_reasoning_effort,
        reasoning_required=request.reasoning_required,
        reasoning_status=request.reasoning_status,
        reasoning_message=request.reasoning_message,
        resume_metadata_json=request.resume_metadata_json,
    )

    gobby_session_id = spawn_context.session_id
    cmd, _cmd_env = build_cli_command(
        cli="droid",
        prompt=request.prompt,
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
    )

    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.api_token:
        env["FACTORY_API_KEY"] = request.api_token
    if request.api_base:
        env["FACTORY_API_BASE_URL"] = request.api_base
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    _record_resume_launch_details(request, agent_run_id=spawn_context.agent_run_id, env=env)

    pre_approve_directory("droid", request.cwd)

    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = terminal_spawner.spawn(
        command=cmd,
        cwd=request.cwd,
        env=env,
    )

    if not terminal_result.success:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=gobby_session_id,
            status="failed",
            error=terminal_result.error or terminal_result.message,
        )

    return SpawnResult(
        success=True,
        run_id=spawn_context.agent_run_id,
        child_session_id=gobby_session_id,
        status="pending",
        pid=terminal_result.pid,
        terminal_type=terminal_result.terminal_type,
        tmux_session_name=terminal_result.tmux_session_name,
        tmux_socket_name=terminal_result.tmux_socket_name,
        tmux_socket_path=terminal_result.tmux_socket_path,
        message=f"Droid agent spawned in terminal with session {gobby_session_id}",
    )
