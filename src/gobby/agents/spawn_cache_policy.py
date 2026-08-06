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
    get_agent_session_cache_dir,
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


def _session_cache_ensurer(dir_name: str) -> Callable[[str], str]:
    def _ensure(session_id: str) -> str:
        cache_dir = get_agent_session_cache_dir(session_id, "gobby", dir_name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return str(cache_dir)

    return _ensure


# Sandboxed spawns lose write access to shared toolchain caches
# (sandbox_policy grants them read-only), so every env-redirectable
# toolchain cache is rerouted to a per-run directory. Non-sandboxed
# spawns keep the shared caches. pnpm's store resolves under $PNPM_HOME;
# npm_config_store_dir is avoided because npm warns on unknown env
# configs. Flag-based redirects (Maven -Dmaven.repo.local, sbt/Ivy
# home flags) are out of env reach and land with #19560's run-root
# split.
SANDBOX_CACHE_POLICY = (
    SpawnCachePolicyEntry("GOCACHE", _session_cache_ensurer("go-cache")),
    SpawnCachePolicyEntry("GOMODCACHE", _session_cache_ensurer("go-mod-cache")),
    SpawnCachePolicyEntry("npm_config_cache", _session_cache_ensurer("npm-cache")),
    SpawnCachePolicyEntry("YARN_CACHE_FOLDER", _session_cache_ensurer("yarn-cache")),
    SpawnCachePolicyEntry("PNPM_HOME", _session_cache_ensurer("pnpm-home")),
    SpawnCachePolicyEntry("PIP_CACHE_DIR", _session_cache_ensurer("pip-cache")),
    SpawnCachePolicyEntry("GRADLE_USER_HOME", _session_cache_ensurer("gradle-home")),
    SpawnCachePolicyEntry("COURSIER_CACHE", _session_cache_ensurer("coursier-cache")),
    SpawnCachePolicyEntry("NUGET_PACKAGES", _session_cache_ensurer("nuget-packages")),
    SpawnCachePolicyEntry("COMPOSER_CACHE_DIR", _session_cache_ensurer("composer-cache")),
    SpawnCachePolicyEntry("PUB_CACHE", _session_cache_ensurer("pub-cache")),
    SpawnCachePolicyEntry("GEM_HOME", _session_cache_ensurer("gem-home")),
    SpawnCachePolicyEntry("BUNDLE_PATH", _session_cache_ensurer("bundle-path")),
    SpawnCachePolicyEntry("HEX_HOME", _session_cache_ensurer("hex-home")),
    SpawnCachePolicyEntry("MIX_HOME", _session_cache_ensurer("mix-home")),
)
SANDBOX_CACHE_ENV_VARS = tuple(entry.env_var for entry in SANDBOX_CACHE_POLICY)


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
    """Include spawned validation caches, registry egress, and hook inbox."""
    if sandbox_config is None:
        return None
    if not sandbox_config.enabled:
        return sandbox_config

    apply_spawn_cache_policy(env_vars)
    _apply_sandbox_cache_policy(env_vars)
    extra_write_paths = list(sandbox_config.extra_write_paths)
    for path in sandbox_write_paths(env_vars):
        if path and path not in extra_write_paths:
            extra_write_paths.append(path)

    return sandbox_config.model_copy(
        update={
            "extra_write_paths": extra_write_paths,
            "allow_package_registries": True,
        }
    )


def _apply_sandbox_cache_policy(env_vars: dict[str, str]) -> None:
    """Materialize per-run toolchain cache redirects for sandboxed spawns."""
    session_id = env_vars.get(GOBBY_SESSION_ID) or "unknown-session"
    for entry in SANDBOX_CACHE_POLICY:
        if not env_vars.get(entry.env_var):
            env_vars[entry.env_var] = entry.ensure_path(session_id)


def sandbox_write_paths(env_vars: dict[str, str]) -> list[str]:
    """Return concrete paths that a sandboxed spawned agent must be able to use."""
    paths = [
        env_vars.get(env_var, "") for env_var in (*SPAWN_CACHE_ENV_VARS, *SANDBOX_CACHE_ENV_VARS)
    ]
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
