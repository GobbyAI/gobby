"""Shared helpers for terminal spawn execution."""

import asyncio
import inspect
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

import psycopg

from gobby.agents.constants import ALL_TERMINAL_ENV_VARS
from gobby.agents.resume_metadata import merge_resume_metadata_env
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

if TYPE_CHECKING:
    from gobby.agents.tmux.spawner import TmuxSpawner

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
    metadata["config_overrides"] = list(config_overrides or [])
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


def _codex_mcp_config_overrides(
    project_path: str | None,
    sandbox_temp_dir: str | None = None,
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
    # Dotted -c overrides replace enough of the spawned server table that Codex
    # no longer sees user-level per-tool approvals. Re-seed only the Gobby proxy
    # tools required by worker contracts so unattended builds do not stop on MCP
    # permission prompts.
    for tool_name in _CODEX_PREAPPROVED_GOBBY_TOOLS:
        overrides.append(f'mcp_servers.gobby.tools.{tool_name}.approval_mode="approve"')
    return overrides
