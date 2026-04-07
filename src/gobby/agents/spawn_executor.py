"""
Unified Spawn Executor for Agent Spawning.

This module consolidates the spawn dispatch logic from agents.py, worktrees.py,
and clones.py into a single executor. All agents spawn via tmux.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.sandbox import (
    GeminiSandboxResolver,
    SandboxConfig,
    compute_sandbox_paths,
)
from gobby.agents.trust import pre_approve_directory

if TYPE_CHECKING:
    from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn import (
    build_cli_command,
    build_codex_command_with_resume,
    prepare_codex_spawn_with_preflight,
    prepare_terminal_spawn,
)
from gobby.agents.tmux.spawner import TmuxSpawner

logger = logging.getLogger(__name__)


@dataclass
class SpawnRequest:
    """Request for spawning an agent."""

    # Required fields
    prompt: str
    cwd: str
    provider: str
    session_id: str
    run_id: str
    parent_session_id: str
    project_id: str

    # Canonical agent_run_id from spawn_agent_impl (pre-generated)
    agent_run_id: str | None = None

    # Optional fields
    workflow: str | None = None
    initial_variables: dict[str, Any] | None = None  # Variables to write to session_variables
    worktree_id: str | None = None
    clone_id: str | None = None
    branch_name: str | None = None  # Git branch for worktree/clone isolation
    task_id: str | None = None  # Task being worked on (for dedup tracking)
    title: str | None = None  # Session title (derived from agent/task name)
    agent_depth: int = 0
    max_agent_depth: int = 5
    session_manager: Any | None = None  # Required for Gemini/Codex preflight
    machine_id: str | None = None
    model: str | None = None  # Model override (e.g., gemini-3-pro-preview)
    api_base: str | None = None  # API base URL for local model endpoints
    api_token: str | None = None  # Auth token for the endpoint

    # Sandbox configuration
    sandbox_config: SandboxConfig | None = None
    sandbox_args: list[str] | None = None
    sandbox_env: dict[str, str] | None = field(default=None)

    # Timeout
    timeout_seconds: float | None = None  # Agent timeout (persisted to DB for restart survival)


@dataclass
class SpawnResult:
    """Result of a spawn operation."""

    success: bool
    run_id: str
    child_session_id: str | None
    status: str

    # Optional result fields
    pid: int | None = None
    terminal_type: str | None = None
    error: str | None = None
    message: str | None = None
    codex_session_id: str | None = None  # Codex external session ID
    tmux_session_name: str | None = None  # Tmux session name for output streaming


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
    elif request.provider == "codex":
        return await _spawn_codex_terminal(request)
    return await _spawn_claude_terminal(request)


async def _spawn_claude_terminal(request: SpawnRequest) -> SpawnResult:
    """
    Spawn Claude agent in terminal with proper session/workflow setup.

    Uses prepare_terminal_spawn to:
    1. Create child session with parent linkage
    2. Pass initial_variables for workflow activation (e.g., assigned_task_id)
    3. Set up environment variables for session matching
    """
    if request.session_manager is None:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=None,
            status="failed",
            error="session_manager is required for Claude spawn",
        )

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
        title=request.title,
        timeout_seconds=request.timeout_seconds,
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
    )

    # Resolve sandbox config if provided
    sandbox_args: list[str] = []
    sandbox_env: dict[str, str] = {}
    if request.sandbox_config and request.sandbox_config.enabled:
        # Claude uses its own sandbox resolver
        from gobby.agents.sandbox import ClaudeSandboxResolver

        resolver = ClaudeSandboxResolver()
        paths = compute_sandbox_paths(
            config=request.sandbox_config,
            workspace_path=request.cwd,
        )
        sandbox_args, sandbox_env = resolver.resolve(request.sandbox_config, paths)
        cmd.extend(sandbox_args)

    # Merge env vars: spawn context + sandbox
    env = spawn_context.env_vars.copy()
    if sandbox_env:
        env.update(sandbox_env)

    # Map api_base/api_token to Claude-specific env vars
    if request.api_base:
        env["ANTHROPIC_BASE_URL"] = request.api_base
    if request.api_token:
        env["ANTHROPIC_AUTH_TOKEN"] = request.api_token

    # Pass machine_id as env var for sandboxed agents
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    # Pre-approve workspace trust so the CLI doesn't show an interactive prompt
    pre_approve_directory("claude", request.cwd)

    # Spawn in terminal with env vars
    terminal_spawner = TmuxSpawner()
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
    if request.session_manager is None:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=None,
            status="failed",
            error="session_manager is required for Gemini spawn",
        )

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
        title=request.title,
        timeout_seconds=request.timeout_seconds,
    )

    gobby_session_id = spawn_context.session_id

    # Build command for fresh Gemini session (not resume)
    # Session context is injected via additionalContext at SessionStart by the daemon
    cmd, _cmd_env = build_cli_command(
        cli="gemini",
        prompt=request.prompt,
        auto_approve=True,
        model=request.model,
    )

    # Resolve sandbox config if provided
    sandbox_args: list[str] = []
    sandbox_env: dict[str, str] = {}
    if request.sandbox_config and request.sandbox_config.enabled:
        resolver = GeminiSandboxResolver()
        paths = compute_sandbox_paths(
            config=request.sandbox_config,
            workspace_path=request.cwd,
        )
        sandbox_args, sandbox_env = resolver.resolve(request.sandbox_config, paths)
        # Append sandbox args to command
        cmd.extend(sandbox_args)

    # Merge env vars: spawn context + sandbox
    env = spawn_context.env_vars.copy()
    if sandbox_env:
        env.update(sandbox_env)

    # Map api_base/api_token to Gemini-specific env vars
    if request.api_base:
        env["GEMINI_API_BASE"] = request.api_base
    if request.api_token:
        env["GEMINI_API_KEY"] = request.api_token

    # Pass machine_id as env var for sandboxed agents that can't read ~/.gobby/machine_id
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    # Pre-approve workspace trust so the CLI doesn't show an interactive prompt
    pre_approve_directory("gemini", request.cwd)

    # Spawn in terminal with env vars
    terminal_spawner = TmuxSpawner()
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
        message=f"Gemini agent spawned in terminal with session {gobby_session_id}",
    )


async def _spawn_codex_terminal(request: SpawnRequest) -> SpawnResult:
    """
    Spawn Codex agent in terminal with preflight session capture.

    Codex outputs session_id in startup banner, which we parse from `codex exec "exit"`.
    """
    if request.session_manager is None:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=request.session_id,
            status="failed",
            error="session_manager is required for Codex preflight",
        )

    try:
        # Preflight capture: gets Codex's session_id and creates linked Gobby session
        spawn_context = await prepare_codex_spawn_with_preflight(
            session_manager=cast("ChildSessionManager", request.session_manager),
            parent_session_id=request.parent_session_id,
            project_id=request.project_id,
            machine_id=request.machine_id or "unknown",
            workflow_name=request.workflow,
            initial_variables=request.initial_variables,
            git_branch=request.branch_name,
        )
    except FileNotFoundError as e:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=request.session_id,
            status="failed",
            error=str(e),
        )
    except Exception as e:
        logger.error(f"Codex preflight capture failed: {e}", exc_info=True)
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=request.session_id,
            status="failed",
            error=f"Codex preflight capture failed: {e}",
        )

    # Extract IDs from prepared spawn context
    gobby_session_id = spawn_context.session_id
    codex_session_id = spawn_context.env_vars["GOBBY_CODEX_EXTERNAL_ID"]

    # Build command with session context injected into prompt
    cmd = build_codex_command_with_resume(
        codex_external_id=codex_session_id,
        prompt=request.prompt,
        auto_approve=True,  # --full-auto for sandboxed autonomy
        gobby_session_id=gobby_session_id,
        working_directory=request.cwd,
        model=request.model,
    )

    # Spawn in terminal
    terminal_spawner = TmuxSpawner()
    terminal_result = terminal_spawner.spawn(
        command=cmd,
        cwd=request.cwd,
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
        run_id=f"codex-{codex_session_id[:8]}",
        child_session_id=gobby_session_id,
        status="pending",
        pid=terminal_result.pid,
        codex_session_id=codex_session_id,
        message=f"Codex agent spawned in terminal with session {gobby_session_id}",
    )
