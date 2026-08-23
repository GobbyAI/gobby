"""Shared helpers for terminal spawn execution."""

import asyncio
import inspect
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

import psycopg

from gobby.agents.constants import (
    ALL_TERMINAL_ENV_VARS,
    GOBBY_AGENT_API_TOKEN,
    GOBBY_AGENT_RUN_ID,
    GOBBY_PROJECT_ID,
    GOBBY_SESSION_ID,
)
from gobby.agents.resume_metadata import (
    filter_resume_config_overrides,
    merge_resume_metadata_env,
)
from gobby.agents.sandbox import coerce_sandbox_config, get_sandbox_resolver
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_cache_policy import (
    PATH_ENV_VAR,
    SPAWN_CACHE_ENV_VARS,
    merge_spawn_path_env,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.spawners.base import SpawnResult as TerminalSpawnResult
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.config.tmux import TmuxConfig
from gobby.sessions.session_wiki_file import redact_session_markdown

if TYPE_CHECKING:
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.agents.tmux.spawner import TmuxSpawner
    from gobby.storage.agents import LocalAgentRunManager

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
_CODEX_REQUIRED_GOBBY_TOOLS = frozenset(
    {
        "list_mcp_servers",
        "list_tools",
        "get_tool_schema",
        "call_tool",
        "get_variable",
        "set_variable",
    }
)
_CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC: int = 360
# GOBBY_PARENT_SESSION_ID is deliberately withheld: the stdio MCP child
# authenticates as the child session, and schema leases must resolve to that
# session, never the parent.
_CODEX_GOBBY_MCP_IDENTITY_ENV_VARS = (
    GOBBY_SESSION_ID,
    GOBBY_PROJECT_ID,
    GOBBY_AGENT_RUN_ID,
)
# Secrets are forwarded by name through Codex's mcp_servers.<id>.env_vars
# whitelist: the value stays in the provider process environment and never
# appears in argv or persisted config overrides.
_CODEX_GOBBY_MCP_FORWARDED_SECRET_ENV_VARS = (GOBBY_AGENT_API_TOKEN,)


def _validate_codex_gobby_tool_allowlist() -> None:
    configured = set(_CODEX_PREAPPROVED_GOBBY_TOOLS)
    if configured != _CODEX_REQUIRED_GOBBY_TOOLS:
        missing = ", ".join(sorted(_CODEX_REQUIRED_GOBBY_TOOLS - configured)) or "none"
        extra = ", ".join(sorted(configured - _CODEX_REQUIRED_GOBBY_TOOLS)) or "none"
        raise RuntimeError(
            f"Codex Gobby MCP preapproval allowlist drifted (missing: {missing}; extra: {extra})"
        )


async def _spawn_terminal(
    spawner: "TmuxSpawner",
    *,
    command: list[str],
    cwd: str,
    env: dict[str, str],
    auth_cli: str,
) -> TerminalSpawnResult:
    if inspect.iscoroutinefunction(getattr(spawner, "spawn_async", None)):
        return await spawner.spawn_async(
            command=command,
            cwd=cwd,
            env=env,
            auth_cli=auth_cli,
        )
    return await asyncio.to_thread(
        spawner.spawn,
        command=command,
        cwd=cwd,
        env=env,
        auth_cli=auth_cli,
    )


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
    mcp_path: str | None = None,
    strict_mcp: bool | None = None,
    sandbox_launch: SandboxLaunch | None = None,
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
    if sandbox_launch is not None:
        metadata["sandbox"] = sandbox_launch.metadata()
    final_env = merge_resume_metadata_env(
        metadata.get("env") if isinstance(metadata.get("env"), Mapping) else None,
        request.extra_env,
        env,
    )
    metadata["env"] = final_env
    metadata["config_overrides"] = filter_resume_config_overrides(config_overrides)
    if mcp_path is not None:
        metadata["mcp_path"] = mcp_path
    if strict_mcp is not None:
        metadata["strict_mcp"] = strict_mcp
    tmux_config = getattr(request.daemon_config, "tmux", None)
    if isinstance(tmux_config, TmuxConfig):
        metadata["tmux_config"] = tmux_config.model_dump()
    try:
        from gobby.storage.agents import LocalAgentRunManager

        LocalAgentRunManager(db).update_resume_metadata(agent_run_id, metadata)
    except psycopg.DatabaseError as exc:
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
    required_methods = (
        "create_child_session",
        "update_terminal_pickup_metadata",
        "update_sandbox_enabled",
        "update_sandbox_policy_hash",
    )
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


def _sandbox_requested(request: SpawnRequest) -> bool:
    return bool(request.sandbox_config and request.sandbox_config.enabled)


def _unsupported_sandbox_request_error(request: SpawnRequest) -> SpawnResult | None:
    if not _sandbox_requested(request):
        return None

    config = coerce_sandbox_config(request.sandbox_config)
    if config is not None and config.backend == "srt":
        return None
    try:
        get_sandbox_resolver(request.provider)
    except ValueError:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=None,
            status="failed",
            error=(
                f"Sandbox requested for provider '{request.provider}', but Gobby has no "
                "sandbox enforcement resolver for that provider. Refusing to spawn unsandboxed."
            ),
        )
    return None


def _record_actual_sandbox_enforcement(
    request: SpawnRequest,
    spawn_context: PreparedSpawn,
    sandbox_launch: SandboxLaunch,
) -> None:
    manager = request.session_manager
    if manager is None:
        return
    manager.update_sandbox_enabled(
        spawn_context.session_id,
        sandbox_launch.enforced,
    )
    if sandbox_launch.policy_hash:
        manager.update_sandbox_policy_hash(
            spawn_context.session_id,
            sandbox_launch.policy_hash,
        )


# Codex 0.147+ cancels in-flight MCP client startup when the first turn
# begins, so a prompt passed as a CLI argument reliably prevents the spawned
# gobby MCP server from registering (the CLI starts that turn at process
# launch). A prompt typed into the composer does not interrupt startup, so
# spawned and resumed Codex terminals receive their prompt as a post-launch
# paste. Delivery must land well inside the session-init watchdog window.
_CODEX_COMPOSER_MARKER = "›"
_CODEX_COMPOSER_CAPTURE_LINES = 40
_CODEX_COMPOSER_POLL_SECONDS = 1.0
_CODEX_COMPOSER_READY_TIMEOUT_SECONDS = 60.0
_CODEX_COMPOSER_SETTLE_SECONDS = 1.0
_CODEX_PROMPT_SUBMIT_RETRY_DELAY_SECONDS = 3.0
_CODEX_PROMPT_DELIVERY_TASKS: set[asyncio.Task[None]] = set()
_CODEX_PROMPT_FAILURE_PANE_MAX_CHARS = 1024
_CODEX_PROMPT_FAILURE_TRUNCATION_MARKER = "[truncated]\n"


def _codex_prompt_failure_reason(pane: str | None) -> str:
    redacted = redact_session_markdown((pane or "").strip()) or "<unavailable>"
    if len(redacted) > _CODEX_PROMPT_FAILURE_PANE_MAX_CHARS:
        tail_chars = _CODEX_PROMPT_FAILURE_PANE_MAX_CHARS - len(
            _CODEX_PROMPT_FAILURE_TRUNCATION_MARKER
        )
        redacted = f"{_CODEX_PROMPT_FAILURE_TRUNCATION_MARKER}{redacted[-tail_chars:]}"
    return (
        "codex_composer_not_ready: Codex composer did not render before "
        f"prompt-delivery timeout\nPane output:\n{redacted}"
    )


def schedule_codex_prompt_delivery(
    tmux: "TmuxSessionManager",
    session_name: str,
    prompt: str,
    run_id: str,
    run_manager: "LocalAgentRunManager | None",
) -> bool:
    """Schedule a typed-paste prompt delivery to a spawned Codex terminal.

    Returns True when a delivery task was scheduled.
    """
    if not prompt:
        return False
    task = asyncio.get_running_loop().create_task(
        _deliver_codex_prompt(tmux, session_name, prompt, run_id, run_manager)
    )
    _CODEX_PROMPT_DELIVERY_TASKS.add(task)
    task.add_done_callback(_CODEX_PROMPT_DELIVERY_TASKS.discard)
    return True


async def _deliver_codex_prompt(
    tmux: "TmuxSessionManager",
    session_name: str,
    prompt: str,
    run_id: str,
    run_manager: "LocalAgentRunManager | None",
) -> None:
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CODEX_COMPOSER_READY_TIMEOUT_SECONDS
        last_pane: str | None = None
        while True:
            try:
                pane = await tmux.capture_pane(
                    session_name,
                    lines=_CODEX_COMPOSER_CAPTURE_LINES,
                )
                if pane:
                    last_pane = pane
            except Exception:
                logger.debug(
                    "Failed to inspect Codex composer readiness for run %s",
                    run_id,
                    exc_info=True,
                )
                pane = None
            if pane and _CODEX_COMPOSER_MARKER in pane:
                break
            if loop.time() >= deadline:
                error = _codex_prompt_failure_reason(last_pane)
                logger.error("Codex prompt delivery failed for run %s: %s", run_id, error)
                try:
                    if run_manager is None:
                        logger.error(
                            "Cannot persist Codex prompt delivery failure for run %s", run_id
                        )
                    else:
                        run_manager.fail(
                            run_id,
                            error=error,
                            tool_calls_count=0,
                            turns_used=0,
                        )
                finally:
                    await tmux.kill_session(session_name, missing_ok=True)
                return
            await asyncio.sleep(_CODEX_COMPOSER_POLL_SECONDS)
        await asyncio.sleep(_CODEX_COMPOSER_SETTLE_SECONDS)
        if not await tmux.send_keys(session_name, f"{prompt}\n", literal=True):
            logger.error(
                "Failed to paste spawn prompt into Codex terminal for run %s",
                run_id,
            )
            return
        # A composer still settling the bracketed paste can swallow the
        # trailing Enter; this follow-up Enter submits in that case and is a
        # no-op on an already-submitted composer.
        await asyncio.sleep(_CODEX_PROMPT_SUBMIT_RETRY_DELAY_SECONDS)
        await tmux.send_keys(session_name, "Enter", literal=False)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Codex prompt delivery failed for run %s", run_id)


def _codex_mcp_config_overrides(
    project_path: str | None,
    sandbox_temp_dir: str | None = None,
    managed_identity_env: Mapping[str, str] | None = None,
) -> list[str]:
    """Force Codex spawned in isolated workspaces to use the main repo MCP server."""
    if not project_path:
        return []
    _validate_codex_gobby_tool_allowlist()
    args = ["run", "--project", project_path, "gobby", "mcp-server"]
    args_toml = "[" + ",".join(json.dumps(arg) for arg in args) + "]"
    overrides = [
        'mcp_servers.gobby.command="uv"',
        f"mcp_servers.gobby.args={args_toml}",
        "mcp_servers.gobby.startup_timeout_sec=120",
        f"mcp_servers.gobby.tool_timeout_sec={_CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC}",
    ]
    # Codex scrubs its own env before launching stdio MCP servers, so the
    # sandbox TMPDIR set on the provider process never reaches this
    # subprocess. Without this it falls back to the platform temp root and
    # writes outside every granted path -- plan-review tools open a
    # TemporaryDirectory here, so that write-then-read must land in the
    # per-run scratchpad the policy actually allows.
    if sandbox_temp_dir:
        overrides.append(f"mcp_servers.gobby.env.TMPDIR={json.dumps(sandbox_temp_dir)}")
    if managed_identity_env:
        for variable_name in _CODEX_GOBBY_MCP_IDENTITY_ENV_VARS:
            if value := managed_identity_env.get(variable_name):
                overrides.append(f"mcp_servers.gobby.env.{variable_name}={json.dumps(value)}")
        # The same scrub drops the session-scoped toolchain cache redirects,
        # and the sandbox policy only grants writes to those redirected roots.
        # Without UV_CACHE_DIR the `uv run` bootstrap aborts against the
        # read-only ~/.cache/uv and the server never starts.
        for variable_name in SPAWN_CACHE_ENV_VARS:
            if value := managed_identity_env.get(variable_name):
                overrides.append(f"mcp_servers.gobby.env.{variable_name}={json.dumps(value)}")
        forwarded_secrets = [
            variable_name
            for variable_name in _CODEX_GOBBY_MCP_FORWARDED_SECRET_ENV_VARS
            if managed_identity_env.get(variable_name)
        ]
        if forwarded_secrets:
            overrides.append(f"mcp_servers.gobby.env_vars={json.dumps(forwarded_secrets)}")
    # Dotted -c overrides replace enough of the spawned server table that Codex
    # no longer sees user-level per-tool approvals. Re-seed only the Gobby proxy
    # tools required by worker contracts so unattended builds do not stop on MCP
    # permission prompts.
    for tool_name in _CODEX_PREAPPROVED_GOBBY_TOOLS:
        overrides.append(f'mcp_servers.gobby.tools.{tool_name}.approval_mode="approve"')
    return overrides
