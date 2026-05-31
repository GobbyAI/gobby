"""Spawned-agent environment and sandbox policy for shared tool/cache paths."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gobby.agents.constants import (
    CARGO_HOME,
    GOBBY_SESSION_ID,
    UV_CACHE_DIR,
    ensure_agent_cargo_home_dir,
    ensure_agent_uv_cache_dir,
)
from gobby.agents.sandbox import SandboxConfig
from gobby.utils.native_bin import native_bin_dir

PATH_ENV_VAR = "PATH"


@dataclass(frozen=True)
class SpawnCachePolicyEntry:
    """One session-scoped writable path exposed to spawned agents."""

    env_var: str
    ensure_path: Callable[[str], str]


SPAWN_CACHE_POLICY = (
    SpawnCachePolicyEntry(UV_CACHE_DIR, ensure_agent_uv_cache_dir),
    SpawnCachePolicyEntry(CARGO_HOME, ensure_agent_cargo_home_dir),
)
SPAWN_CACHE_ENV_VARS = tuple(entry.env_var for entry in SPAWN_CACHE_POLICY)


def managed_tool_bin_dir() -> str:
    """Return Gobby's managed native-tool directory."""
    return str(native_bin_dir())


def hook_inbox_dir() -> str:
    """Return the daemon-owned hook inbox directory."""
    return str(Path.home() / ".gobby" / "hooks" / "inbox")


def build_spawn_cache_env(session_id: str) -> dict[str, str]:
    """Return env values for shared spawned-agent cache and tool paths."""
    env = {entry.env_var: entry.ensure_path(session_id) for entry in SPAWN_CACHE_POLICY}
    env[PATH_ENV_VAR] = merge_spawn_path(os.environ.get(PATH_ENV_VAR))
    return env


def apply_spawn_cache_policy(env_vars: dict[str, str]) -> None:
    """Materialize missing cache/tool env vars in an existing spawn env."""
    session_id = env_vars.get(GOBBY_SESSION_ID) or "unknown-session"
    for entry in SPAWN_CACHE_POLICY:
        if not env_vars.get(entry.env_var):
            env_vars[entry.env_var] = entry.ensure_path(session_id)
    env_vars[PATH_ENV_VAR] = merge_spawn_path(env_vars.get(PATH_ENV_VAR))


def merge_spawn_path(preferred_path: str | None, base_path: str | None = None) -> str:
    """Merge PATH values while keeping isolated gcode wrappers ahead of managed tools."""
    entries = _split_path(preferred_path)
    entries.extend(
        _split_path(base_path if base_path is not None else os.environ.get(PATH_ENV_VAR))
    )
    return os.pathsep.join(_insert_managed_tool_bin(_dedupe(entries)))


def merge_spawn_path_env(env_vars: dict[str, str], preferred_path: str) -> None:
    """Merge an incoming PATH override into an env dict without dropping managed tools."""
    env_vars[PATH_ENV_VAR] = merge_spawn_path(preferred_path, env_vars.get(PATH_ENV_VAR))


def sandbox_config_for_spawn(
    sandbox_config: SandboxConfig | None,
    env_vars: dict[str, str],
) -> SandboxConfig | None:
    """Include spawned validation caches and hook inbox in sandbox writable paths."""
    if sandbox_config is None:
        return None
    if not sandbox_config.enabled:
        return sandbox_config

    apply_spawn_cache_policy(env_vars)
    extra_write_paths = list(sandbox_config.extra_write_paths)
    for path in sandbox_write_paths(env_vars):
        if path and path not in extra_write_paths:
            extra_write_paths.append(path)

    return sandbox_config.model_copy(update={"extra_write_paths": extra_write_paths})


def sandbox_write_paths(env_vars: dict[str, str]) -> list[str]:
    """Return concrete paths that a sandboxed spawned agent must be able to use."""
    paths = [env_vars.get(env_var, "") for env_var in SPAWN_CACHE_ENV_VARS]
    paths.append(hook_inbox_dir())
    return _dedupe(paths)


def _split_path(path_value: str | None) -> list[str]:
    if not path_value:
        return []
    return [entry for entry in path_value.split(os.pathsep) if entry]


def _dedupe(entries: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        deduped.append(entry)
    return deduped


def _insert_managed_tool_bin(entries: list[str]) -> list[str]:
    managed_bin = managed_tool_bin_dir()
    entries = [entry for entry in entries if entry != managed_bin]
    insert_at = 0
    while insert_at < len(entries) and _is_isolated_gobby_bin(entries[insert_at], managed_bin):
        insert_at += 1
    entries.insert(insert_at, managed_bin)
    return entries


def _is_isolated_gobby_bin(path_text: str, managed_bin: str) -> bool:
    if path_text == managed_bin:
        return False
    path = Path(path_text).expanduser()
    return path.name == "bin" and path.parent.name == ".gobby"
