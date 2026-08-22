"""
Unified Spawn Executor for Agent Spawning.

This module consolidates the spawn dispatch logic from agents.py, worktrees.py,
and clones.py into a single executor. All agents spawn via tmux.
"""

import logging
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.agents.constants import (
    GOBBY_AGENT_API_TOKEN,
    GOBBY_AGENT_RUN_ID,
    GOBBY_PROJECT_ID,
    GOBBY_SESSION_ID,
)
from gobby.agents.isolation_code_index import ensure_isolation_code_index
from gobby.agents.sandbox import get_sandbox_resolver
from gobby.agents.spawn import (
    PreparedSpawn,
    build_cli_command,
)
from gobby.agents.spawn import (
    prepare_terminal_spawn as prepare_terminal_spawn,
)
from gobby.agents.spawn_cache_policy import (
    sandbox_config_for_spawn as _sandbox_config_for_spawn,
)
from gobby.agents.spawn_executor_support import (
    _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC,
    _CODEX_PREAPPROVED_GOBBY_TOOLS,
    _apply_extra_env,
    _codex_mcp_config_overrides,
    _record_actual_sandbox_enforcement,
    _record_resume_launch_details,
    _session_manager_validation_error,
    _spawn_terminal,
    _unsupported_sandbox_request_error,
    schedule_codex_prompt_delivery,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.srt_runtime import (
    SandboxLaunch,
    SrtRuntimeError,
    prepare_sandbox_launch,
)
from gobby.agents.trust import pre_approve_directory
from gobby.providers import AGY_UNAVAILABLE_REASON
from gobby.providers.capabilities.apply import speed_result

if TYPE_CHECKING:
    pass
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.config.tmux import TmuxConfig

# Gobby-managed Claude agents must use Gobby's spawn/session controls. Native Claude
# delegation bypasses project context, depth limits, sandbox metadata, and task ownership.
_CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS = ["Workflow", "Task"]
_NATIVE_SUBAGENT_RESEARCH_AGENTS = frozenset({"plan-adversary", "plan-adversary-taskless"})

logger = logging.getLogger(__name__)

__all__ = [
    "SpawnRequest",
    "SpawnResult",
    "execute_spawn",
]

_COMPAT_PRIVATE_EXPORTS = (
    _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC,
    _CODEX_PREAPPROVED_GOBBY_TOOLS,
)


def _tmux_spawner_for_request(request: SpawnRequest) -> TmuxSpawner:
    daemon_config = request.daemon_config
    tmux_config = getattr(daemon_config, "tmux", None)
    if not isinstance(tmux_config, TmuxConfig):
        tmux_config = TmuxConfig()

    return TmuxSpawner(config=tmux_config)


async def _prepare_provider_sandbox(
    request: SpawnRequest,
    spawn_context: PreparedSpawn,
    provider: str,
    env: dict[str, str],
) -> SandboxLaunch | SpawnResult:
    config = _sandbox_config_for_spawn(request.sandbox_config, env)
    if config is None:
        launch = SandboxLaunch(backend="provider-native", enforced=False)
        _record_actual_sandbox_enforcement(request, spawn_context, launch)
        return launch
    resolver = None
    if config.enabled and config.backend == "provider-native":
        resolver = get_sandbox_resolver(provider)
    daemon_port = int(getattr(request.daemon_config, "daemon_port", 60887))
    websocket = getattr(request.daemon_config, "websocket", None)
    websocket_port = int(getattr(websocket, "port", 60888))
    try:
        launch = await prepare_sandbox_launch(
            config=config,
            provider=provider,
            workspace_path=request.cwd,
            run_id=spawn_context.agent_run_id,
            resolver=resolver,
            daemon_port=daemon_port,
            websocket_port=websocket_port,
            api_base=request.api_base,
            env=env,
        )
    except (OSError, ValueError, SrtRuntimeError) as exc:
        error = f"Sandbox startup failed closed for {provider}: {exc}"
        if request.run_manager is not None:
            request.run_manager.fail(spawn_context.agent_run_id, error)
        return SpawnResult(
            success=False,
            run_id=spawn_context.agent_run_id,
            child_session_id=spawn_context.session_id,
            status="failed",
            error=error,
        )
    env.update(launch.provider_env)
    _record_actual_sandbox_enforcement(request, spawn_context, launch)
    return launch


async def _prepare_managed_code_index(
    request: SpawnRequest,
    spawn_context: PreparedSpawn,
) -> SpawnResult | None:
    mode = request.code_index_preflight_mode
    if mode is None:
        return None
    try:
        credential = spawn_context.managed_credential
        if credential is None:
            raise RuntimeError("managed credential unavailable for code index preflight")
        # gcore's effective-config resolution requires the spawned run's
        # managed-execution identity; the daemon's own env carries none.
        identity_env = {
            name: value
            for name, value in spawn_context.env_vars.items()
            if name in (GOBBY_AGENT_RUN_ID, GOBBY_PROJECT_ID, GOBBY_SESSION_ID) and value
        }
        # Isolation gcode uses the run-scoped managed credential; the operator
        # token is only a tokenless-dev fallback when no run token was minted.
        run_api_token = spawn_context.env_vars.get(GOBBY_AGENT_API_TOKEN)
        preflight = await ensure_isolation_code_index(
            request.cwd,
            credential=credential,
            api_token=run_api_token or request.code_index_api_token,
            identity_env=identity_env,
        )
        spawn_context.env_vars.update(preflight.env)
        return None
    except Exception as exc:
        if mode == "required":
            return SpawnResult(
                success=False,
                run_id=request.run_id,
                child_session_id=spawn_context.session_id,
                status="failed",
                error=f"planner_code_index_unavailable:{exc}",
            )
        warning = {
            "preflight": "code_index",
            "cwd": request.cwd,
            "message": str(exc),
        }
        request.code_index_preflight_warning = warning
        request.prompt = _append_code_index_warning(request.prompt, warning)
        if request.initial_variables is not None:
            skills = request.initial_variables.get("additional_skills", [])
            if isinstance(skills, list):
                request.initial_variables["additional_skills"] = [
                    skill for skill in skills if skill != "code-index"
                ]
            request.initial_variables["code_index_preflight_warning"] = warning
            request.initial_variables["prompt"] = request.prompt
            if request.session_manager is not None:
                from gobby.workflows.state_manager import SessionVariableManager

                SessionVariableManager(request.session_manager._storage.db).merge_variables(
                    spawn_context.session_id,
                    request.initial_variables,
                )
        logging.getLogger(__name__).warning(
            "Continuing spawn after scoped code index preflight failed for cwd=%s: %s",
            request.cwd,
            exc,
        )
        return None


def _agent_prompt_prefix(request: SpawnRequest) -> str:
    """Resolve the spawned agent's execution preamble for prompt assembly.

    Codex delivers hook-injected context after the composer prompt, so the
    agent block used to trail the task prompt (#20451). Prepending it at
    spawn assembly puts identity before the task. Only an explicitly requested
    agent is resolved here — activation may pick a configured default agent,
    and guessing it at spawn time risks front-loading the wrong persona.
    """
    if request.session_manager is None:
        return ""
    agent_name = request.agent_name or (request.initial_variables or {}).get("_agent_type")
    if not agent_name:
        return ""
    try:
        from gobby.workflows.agent_resolver import resolve_agent

        agent_body = resolve_agent(
            str(agent_name),
            request.session_manager._storage.db,
            project_id=request.project_id,
        )
    except Exception:
        logger.debug("Prompt resolution failed for spawn agent %r", agent_name, exc_info=True)
        return ""
    if not agent_body:
        return ""
    return agent_body.prompt_for("agent") or ""


def _append_code_index_warning(prompt: str, warning: dict[str, str]) -> str:
    message = warning.get("message", "unknown")
    return (
        f"{prompt}\n\n---\n\n"
        "## Code Index\n"
        "Use standard file search and read tools for code navigation in this isolated "
        f"workspace. Code-index preflight failed: {message}"
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
    result = _unsupported_sandbox_request_error(request)
    if result is None:
        if request.provider == "claude" and request.agent_name in _NATIVE_SUBAGENT_RESEARCH_AGENTS:
            logger.warning(
                "Agent %s requests provider-native internal subagents, but the managed "
                "Claude runtime strips the native Task facility; internal research lanes "
                "will be unavailable",
                request.agent_name,
            )

        if request.provider == "grok":
            result = await _spawn_grok_terminal(request)
        elif request.provider == "qwen":
            result = await _spawn_qwen_terminal(request)
        elif request.provider == "codex":
            result = await _spawn_codex_terminal(request)
        elif request.provider == "droid":
            result = await _spawn_droid_terminal(request)
        elif request.provider == "agy":
            result = SpawnResult(
                success=False,
                run_id=request.run_id,
                child_session_id=None,
                status="failed",
                error=AGY_UNAVAILABLE_REASON,
            )
        elif request.provider == "claude":
            result = await _spawn_claude_terminal(request)
        else:
            result = SpawnResult(
                success=False,
                run_id=request.run_id,
                child_session_id=None,
                status="failed",
                error=f"Unsupported spawn provider: {request.provider}",
            )

    if request.speed_resolution is not None:
        result = replace(result, speed=speed_result(request.speed_resolution))
    return result


async def _spawn_claude_terminal(request: SpawnRequest) -> SpawnResult:
    """
    Spawn Claude agent in terminal with proper session/workflow setup.

    Uses the caller-prepared spawn so the child session already exists.
    """
    if validation_error := _session_manager_validation_error(request, "Claude"):
        return validation_error

    spawn_context = request.prepared_spawn

    gobby_session_id = spawn_context.session_id

    # Build command for Claude CLI
    # Pass session_id so Claude uses --session-id flag, which allows the
    # SessionStart hook to match this process to the pre-created session
    # (and auto-activate the workflow, which delivers the prompt via on_enter).
    cmd, _cmd_env = build_cli_command(
        cli="claude",
        prompt=None,
        session_id=gobby_session_id,
        auto_approve=True,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        disallowed_tools=_CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS,
    )
    claude_mcp_config_path = Path(request.cwd) / ".mcp.json"
    claude_mcp_config_arg: str | None = None
    strict_mcp = False
    if claude_mcp_config_path.exists():
        claude_mcp_config_arg = str(claude_mcp_config_path)
        strict_mcp = True
        cmd.extend(["--mcp-config", claude_mcp_config_arg, "--strict-mcp-config"])

    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    # Gobby owns memory for spawned agents; keep the harness-level auto-memory
    # section out of their system prompts regardless of user settings.json.
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if request.api_base:
        env["ANTHROPIC_BASE_URL"] = request.api_base
    if request.api_token:
        env["ANTHROPIC_AUTH_TOKEN"] = request.api_token
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "claude", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result
    if launch.enforced and launch.backend == "srt":
        # Claude's own Bash sandbox cannot nest inside the SRT seatbelt;
        # CLI --settings outranks user settings, so pin it off regardless
        # of ~/.claude/settings.json. SRT is the authoritative boundary.
        cmd.extend(["--settings", '{"sandbox": {"enabled": false}}'])
    cmd.extend(launch.provider_args)
    if request.prompt:
        cmd.append(request.prompt)
    cmd = launch.wrap(cmd)

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        mcp_path=claude_mcp_config_arg,
        strict_mcp=strict_mcp,
        sandbox_launch=launch,
    )

    # Pre-approve workspace trust so the CLI doesn't show an interactive prompt
    pre_approve_directory("claude", request.cwd)

    # Spawn in terminal with env vars
    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = await _spawn_terminal(
        terminal_spawner,
        command=cmd,
        cwd=request.cwd,
        env=env,
        auth_cli="claude",
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

    spawn_context = request.prepared_spawn

    gobby_session_id = spawn_context.session_id

    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.api_base:
        env["QWEN_API_BASE"] = request.api_base
    if request.api_token:
        env["QWEN_API_KEY"] = request.api_token
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "qwen", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result

    cmd, _cmd_env = build_cli_command(
        cli="qwen",
        prompt=request.prompt,
        auto_approve=True,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=launch.provider_args or None,
    )
    cmd = launch.wrap(cmd)

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        sandbox_launch=launch,
    )

    pre_approve_directory("qwen", request.cwd)

    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = await _spawn_terminal(
        terminal_spawner,
        command=cmd,
        cwd=request.cwd,
        env=env,
        auth_cli="qwen",
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

    spawn_context = request.prepared_spawn

    gobby_session_id = spawn_context.session_id
    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.api_base:
        env["GROK_API_BASE"] = request.api_base
    if request.api_token:
        env["XAI_API_KEY"] = request.api_token
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "grok", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result

    cmd, _cmd_env = build_cli_command(
        cli="grok",
        prompt=request.prompt,
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=launch.provider_args or None,
    )
    cmd = launch.wrap(cmd)

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        sandbox_launch=launch,
    )

    pre_approve_directory("grok", request.cwd)

    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = await _spawn_terminal(
        terminal_spawner,
        command=cmd,
        cwd=request.cwd,
        env=env,
        auth_cli="grok",
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

    Session linkage approach:
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

    spawn_context = request.prepared_spawn

    gobby_session_id = spawn_context.session_id
    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.api_base:
        env["OPENAI_BASE_URL"] = request.api_base
    if request.api_token:
        env["OPENAI_API_KEY"] = request.api_token
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "codex", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result

    config_overrides = [
        *_codex_mcp_config_overrides(
            request.project_path,
            (launch.provider_env or {}).get("TMPDIR"),
            managed_identity_env=env,
        ),
        *request.codex_config_overrides,
    ]
    if launch.enforced and launch.backend == "srt":
        # Codex's internal seatbelt cannot nest inside the SRT sandbox
        # (sandbox_apply: Operation not permitted kills its shell and
        # node_repl kernels). SRT is the authoritative boundary.
        config_overrides.append('sandbox_mode="danger-full-access"')
    cmd, _cmd_env = build_cli_command(
        cli="codex",
        # Never pass the prompt as a CLI argument: Codex 0.147+ starts the
        # first turn at launch and cancels in-flight MCP client startup,
        # which kills gobby MCP registration. The prompt is typed into the
        # composer after launch instead (schedule_codex_prompt_delivery).
        prompt="",
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        codex_oss_provider=request.codex_oss_provider,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=launch.provider_args or None,
        config_overrides=config_overrides,
    )
    cmd = launch.wrap(cmd)

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        config_overrides=config_overrides,
        sandbox_launch=launch,
    )

    pre_approve_directory("codex", request.cwd)

    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = await _spawn_terminal(
        terminal_spawner,
        command=cmd,
        cwd=request.cwd,
        env=env,
        auth_cli="codex",
    )

    if not terminal_result.success:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=gobby_session_id,
            status="failed",
            error=terminal_result.error or terminal_result.message,
        )

    if terminal_result.tmux_session_name:
        prompt_text = request.prompt or ""
        agent_prompt = _agent_prompt_prefix(request)
        if agent_prompt and request.session_manager is not None:
            prompt_text = f"{agent_prompt}\n\n{prompt_text}" if prompt_text else agent_prompt
            from gobby.workflows.state_manager import SessionVariableManager

            # Suppress first-turn agent prompt injection — the preamble already
            # precedes the task prompt in the composer text.
            SessionVariableManager(request.session_manager._storage.db).merge_variables(
                gobby_session_id,
                {"_agent_context_injected": True},
            )
        schedule_codex_prompt_delivery(
            terminal_spawner.session_manager,
            terminal_result.tmux_session_name,
            prompt_text,
            spawn_context.agent_run_id,
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

    spawn_context = request.prepared_spawn

    gobby_session_id = spawn_context.session_id
    if preflight_error := await _prepare_managed_code_index(request, spawn_context):
        return preflight_error
    env = spawn_context.env_vars.copy()
    _apply_extra_env(env, request)
    if request.api_token:
        env["FACTORY_API_KEY"] = request.api_token
    if request.api_base:
        env["FACTORY_API_BASE_URL"] = request.api_base
    if request.machine_id:
        env["GOBBY_MACHINE_ID"] = request.machine_id

    sandbox_result = await _prepare_provider_sandbox(request, spawn_context, "droid", env)
    if isinstance(sandbox_result, SpawnResult):
        return sandbox_result
    launch = sandbox_result
    cmd, _cmd_env = build_cli_command(
        cli="droid",
        prompt=request.prompt,
        auto_approve=True,
        working_directory=request.cwd,
        model=request.model,
        reasoning_effort=request.effective_reasoning_effort,
        sandbox_args=launch.provider_args or None,
    )
    cmd = launch.wrap(cmd)

    _record_resume_launch_details(
        request,
        agent_run_id=spawn_context.agent_run_id,
        sandbox_args=launch.provider_args,
        sandbox_env=launch.provider_env,
        env=env,
        sandbox_launch=launch,
    )

    pre_approve_directory("droid", request.cwd)

    terminal_spawner = _tmux_spawner_for_request(request)
    terminal_result = await _spawn_terminal(
        terminal_spawner,
        command=cmd,
        cwd=request.cwd,
        env=env,
        auth_cli="droid",
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
