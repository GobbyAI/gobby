"""Sandbox configuration helpers for spawned CLIs.

This module keeps the provider-specific sandbox contract in one place so the
same rules can be reused by terminal spawns, web-chat backends, and any future
installer/runtime glue that needs to materialize provider settings.
"""

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, PrivateAttr

from gobby.utils.daemon_git import GitFailed, GitOk, daemon_git


class SandboxConfig(BaseModel):
    """
    Configuration for sandbox/isolation when spawning agents.

    This is opt-in - by default sandboxing is disabled to preserve
    existing behavior. When enabled, the appropriate CLI flags are
    passed to enable the CLI's built-in sandbox.

    Attributes:
        enabled: Whether to enable sandboxing. Default False.
        mode: Sandbox strictness level.
            - "permissive": Allow more operations (easier debugging)
            - "restrictive": Stricter isolation (more secure)
        allow_network: Whether to allow network access. Daemon-owned spawned
            agents keep this enabled so local Gobby services on loopback remain
            reachable.
        extra_read_paths: Additional paths to allow read access.
        extra_write_paths: Additional paths to allow write access
            (worktree paths are always allowed).
    """

    enabled: bool = False
    backend: Literal["srt", "provider-native"] = "provider-native"
    mode: Literal["permissive", "restrictive"] = "permissive"
    allow_network: bool = True
    extra_read_paths: list[str] = Field(default_factory=list)
    extra_write_paths: list[str] = Field(default_factory=list)
    extra_deny_read_paths: list[str] = Field(default_factory=list)
    extra_deny_write_paths: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)
    allow_git_network: bool = False
    allow_package_registries: bool = False
    allow_unix_sockets: list[str] = Field(default_factory=list)
    # Launch authority, recomputed from the stored session/definition. Never accepted
    # from user config or persisted in a policy snapshot.
    _workspace_reads: tuple[str, ...] = PrivateAttr(default=())
    _workspace_writes: tuple[str, ...] = PrivateAttr(default=())

    def with_workspace_scope(self, reads: list[str], writes: list[str]) -> "SandboxConfig":
        scoped = self.model_copy(deep=True)
        scoped._workspace_reads = tuple(reads)
        scoped._workspace_writes = tuple(writes)
        return scoped

    def restrict_workspace_paths(self, reads: list[str], writes: list[str]) -> list[str]:
        """Remove read exceptions that would defeat the workspace root denial.

        Linux restores write binds under read-denied roots, so write grants must
        independently stay within the seat's writable scope on every platform.
        """
        from gobby.agents.agent_workspaces import workspaces_root

        root = workspaces_root().resolve()

        def within(path: Path, parent: Path) -> bool:
            # A case-only alias can override a Seatbelt deny on case-insensitive
            # volumes. Conservatively discard it even when it cannot be stat'ed.
            return tuple(part.casefold() for part in path.parts[: len(parent.parts)]) == tuple(
                part.casefold() for part in parent.parts
            )

        def checked_scope(items: tuple[str, ...]) -> list[Path]:
            scope = []
            for item in items:
                if any(char in item for char in "*?[{"):
                    raise ValueError(f"agent workspace scope contains glob syntax: {item}")
                path = Path(item)
                relative = path.relative_to(workspaces_root())
                expected = root / relative
                if path.resolve() != expected:
                    raise ValueError(f"agent workspace scope follows an alias: {path}")
                scope.append(expected)
            return scope

        allowed_reads = checked_scope(self._workspace_reads)
        allowed_writes = checked_scope(self._workspace_writes)
        for item in writes:
            path = Path(item)
            if (within(path, root) or within(root, path)) and not any(
                path.is_relative_to(folder) for folder in allowed_writes
            ):
                raise ValueError(f"write grant exceeds agent workspace scope: {path}")
        retained = [
            item for item in reads if not (within(Path(item), root) or within(root, Path(item)))
        ]
        return list(dict.fromkeys([*retained, *(str(path) for path in allowed_reads)]))


_DAEMON_OWNED_SANDBOX_MODE: Literal["permissive", "restrictive"] = "permissive"
_DAEMON_SANDBOX_POLICY_VERSION = 2
# Spawn-time git reads run on loaded hosts; 2 s failed closed. Matches daemon_git.run default.
_GIT_METADATA_TIMEOUT_SECONDS = 10.0
_WEB_CHAT_POLICY_MISMATCH_MESSAGE = (
    "This chat was created under a different sandbox policy. Continue it in a new chat."
)


def coerce_sandbox_config(config: Any | None) -> SandboxConfig | None:
    """Normalize a config-like object into SandboxConfig."""
    if config is None:
        return None
    if isinstance(config, SandboxConfig):
        return config.model_copy(deep=True)
    if isinstance(config, dict):
        return SandboxConfig(**config)

    raw_backend = str(getattr(config, "backend", "provider-native"))
    backend = cast(
        Literal["srt", "provider-native"],
        raw_backend if raw_backend in {"srt", "provider-native"} else "provider-native",
    )
    raw_mode = str(getattr(config, "mode", "permissive"))
    mode = cast(
        Literal["permissive", "restrictive"],
        raw_mode if raw_mode in {"permissive", "restrictive"} else "permissive",
    )

    return SandboxConfig(
        enabled=bool(getattr(config, "enabled", False)),
        backend=backend,
        mode=mode,
        allow_network=bool(getattr(config, "allow_network", True)),
        extra_read_paths=list(getattr(config, "extra_read_paths", []) or []),
        extra_write_paths=list(getattr(config, "extra_write_paths", []) or []),
        extra_deny_read_paths=list(getattr(config, "extra_deny_read_paths", []) or []),
        extra_deny_write_paths=list(getattr(config, "extra_deny_write_paths", []) or []),
        allowed_domains=list(getattr(config, "allowed_domains", []) or []),
        denied_domains=list(getattr(config, "denied_domains", []) or []),
        allow_git_network=bool(getattr(config, "allow_git_network", False)),
        allow_package_registries=bool(getattr(config, "allow_package_registries", False)),
        allow_unix_sockets=list(getattr(config, "allow_unix_sockets", []) or []),
    )


def daemon_owned_sandbox_config(
    config: Any | None,
    *,
    default_enabled: bool = True,
    default_allow_network: bool = False,
) -> SandboxConfig:
    """Resolve daemon-owned sandbox config into the internal runtime model."""
    if config is None:
        return SandboxConfig(
            enabled=default_enabled,
            backend="srt",
            mode=_DAEMON_OWNED_SANDBOX_MODE,
            allow_network=default_allow_network,
        )

    raw_mode = getattr(config, "mode", _DAEMON_OWNED_SANDBOX_MODE)
    mode = cast(
        Literal["permissive", "restrictive"],
        raw_mode
        if isinstance(raw_mode, str) and raw_mode in {"permissive", "restrictive"}
        else _DAEMON_OWNED_SANDBOX_MODE,
    )
    raw_allow_network = getattr(config, "allow_network", default_allow_network)
    raw_backend = getattr(config, "backend", "srt")
    backend = cast(
        Literal["srt", "provider-native"],
        raw_backend
        if isinstance(raw_backend, str) and raw_backend in {"srt", "provider-native"}
        else "srt",
    )

    return SandboxConfig(
        enabled=bool(getattr(config, "enabled", default_enabled)),
        backend=backend,
        mode=mode,
        allow_network=(
            raw_allow_network if isinstance(raw_allow_network, bool) else default_allow_network
        ),
        extra_read_paths=list(getattr(config, "extra_read_paths", []) or []),
        extra_write_paths=list(getattr(config, "extra_write_paths", []) or []),
        extra_deny_read_paths=list(getattr(config, "extra_deny_read_paths", []) or []),
        extra_deny_write_paths=list(getattr(config, "extra_deny_write_paths", []) or []),
        allowed_domains=list(getattr(config, "allowed_domains", []) or []),
        denied_domains=list(getattr(config, "denied_domains", []) or []),
        allow_git_network=bool(getattr(config, "allow_git_network", False)),
        allow_package_registries=bool(getattr(config, "allow_package_registries", False)),
        allow_unix_sockets=list(getattr(config, "allow_unix_sockets", []) or []),
    )


def web_chat_sandbox_config(daemon_config: Any | None) -> SandboxConfig:
    """Return the daemon-owned web-chat sandbox config."""
    raw_config = getattr(daemon_config, "web_chat_sandbox", None) if daemon_config else None
    return daemon_owned_sandbox_config(
        raw_config,
        default_enabled=True,
        default_allow_network=False,
    )


def agent_sandbox_config(daemon_config: Any | None) -> SandboxConfig:
    """Return the daemon-owned spawned-agent sandbox config."""
    raw_config = getattr(daemon_config, "agent_sandbox", None) if daemon_config else None
    return daemon_owned_sandbox_config(
        raw_config,
        default_enabled=True,
        default_allow_network=False,
    )


def daemon_owned_sandbox_policy_hash(
    config: Any | None,
    *,
    scope: str,
    default_enabled: bool = True,
) -> str:
    """Return a stable hash for daemon-owned sandbox policy snapshots."""
    resolved = daemon_owned_sandbox_config(config, default_enabled=default_enabled)
    payload = {
        "version": _DAEMON_SANDBOX_POLICY_VERSION,
        "scope": scope,
        "enabled": resolved.enabled,
        "backend": resolved.backend,
        "mode": resolved.mode,
        "allow_network": resolved.allow_network,
        "extra_read_paths": resolved.extra_read_paths,
        "extra_write_paths": resolved.extra_write_paths,
        "extra_deny_read_paths": resolved.extra_deny_read_paths,
        "extra_deny_write_paths": resolved.extra_deny_write_paths,
        "allowed_domains": resolved.allowed_domains,
        "denied_domains": resolved.denied_domains,
        "allow_git_network": resolved.allow_git_network,
        "allow_package_registries": resolved.allow_package_registries,
        "allow_unix_sockets": resolved.allow_unix_sockets,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def web_chat_sandbox_policy_hash(daemon_config: Any | None) -> str:
    """Return the current daemon-owned web-chat sandbox policy hash."""
    raw_config = getattr(daemon_config, "web_chat_sandbox", None) if daemon_config else None
    return daemon_owned_sandbox_policy_hash(raw_config, scope="web_chat", default_enabled=True)


def web_chat_policy_mismatch_message() -> str:
    """Return the standard web-chat sandbox policy mismatch message."""
    return _WEB_CHAT_POLICY_MISMATCH_MESSAGE


class SandboxCredentialEnv(BaseModel):
    """Credential environment variable handled by the host sandbox."""

    name: str
    mode: Literal["deny", "mask"] = "mask"
    inject_hosts: list[str] = Field(default_factory=list)


class ResolvedSandboxPaths(BaseModel):
    """
    Resolved paths and settings for sandbox execution.

    This is the computed result after resolving a SandboxConfig against
    the actual workspace and daemon configuration. It contains the concrete
    paths and settings that will be passed to CLI sandbox flags.

    Attributes:
        workspace_path: The primary workspace/worktree path for the agent.
        gobby_daemon_port: Port where Gobby daemon is running (for network allowlist).
        read_paths: All paths the sandbox should allow read access to.
        write_paths: All paths the sandbox should allow write access to.
        allow_external_network: Whether to allow network access beyond localhost.
    """

    workspace_path: str
    gobby_daemon_port: int = 60887
    read_paths: list[str]
    write_paths: list[str]
    allow_external_network: bool
    deny_read_paths: list[str] = Field(default_factory=list)
    deny_write_paths: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)
    loopback_ports: list[int] = Field(default_factory=list)
    allow_unix_sockets: list[str] = Field(default_factory=list)
    credential_env_vars: list[SandboxCredentialEnv] = Field(default_factory=list)
    provider: str | None = None


async def compute_sandbox_paths(
    config: SandboxConfig,
    workspace_path: str,
    gobby_daemon_port: int = 60887,
    *,
    gobby_websocket_port: int = 60888,
    provider: str | None = None,
    provider_executable: str | None = None,
    api_base: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedSandboxPaths:
    """
    Compute resolved sandbox paths from a SandboxConfig.

    This helper function combines the workspace path with extra paths
    from the config to produce the final ResolvedSandboxPaths.

    Args:
        config: The sandbox configuration.
        workspace_path: The primary workspace/worktree path.
        gobby_daemon_port: Port where Gobby daemon is running.

    Returns:
        ResolvedSandboxPaths with all paths computed.
    """
    from gobby.agents.sandbox_policy import (
        allowed_domains,
        assert_sensitive_path_contract,
        canonical_path,
        canonical_paths,
        credential_env_vars,
        credential_read_roots,
        darwin_toolchain_write_paths,
        default_write_paths,
        deny_paths,
        gcode_runtime_write_exceptions,
        linked_auth_home_entries,
        provider_credential_read_exceptions,
        provider_read_exceptions,
        provider_write_exceptions,
        sensitive_roots,
        sensitive_write_roots,
        toolchain_credential_paths,
        toolchain_read_roots,
    )
    from gobby.agents.sandbox_read_roots import gobby_read_exceptions

    workspace = Path(canonical_path(workspace_path))
    policy_env = os.environ if env is None else env
    from gobby.agents.sandbox_control_paths import (
        assert_credential_read_contract,
        provider_control_write_paths,
    )

    provider_reads = (
        provider_read_exceptions(provider, policy_env, provider_executable=provider_executable)
        if provider
        else []
    )
    credential_denies = credential_read_roots()
    git_paths = await _git_metadata_write_paths(workspace)
    from gobby.integrations.rtk import sandbox_paths as resolve_rtk_sandbox_paths

    rtk_paths = resolve_rtk_sandbox_paths(env=policy_env)
    write_paths = canonical_paths(
        [
            *default_write_paths(config, workspace),
            *darwin_toolchain_write_paths(),
            *git_paths,
            *gcode_runtime_write_exceptions(workspace),
            *(provider_write_exceptions(provider) if provider else []),
            *(tuple(str(path) for path in rtk_paths.write_paths) if rtk_paths else ()),
        ]
    )
    read_paths = canonical_paths(
        [
            str(workspace),
            *write_paths,
            *gobby_read_exceptions(policy_env),
            *(tuple(str(path) for path in rtk_paths.read_paths) if rtk_paths else ()),
            *toolchain_read_roots(),
            *provider_reads,
            "~/.ssh/known_hosts",
            *config.extra_read_paths,
        ],
        base=workspace,
    )
    read_paths.extend(linked_auth_home_entries(provider) if provider else [])
    deny_read_paths = deny_paths(
        [
            *sensitive_roots(),
            *credential_denies,
            *toolchain_credential_paths(),
            *config.extra_deny_read_paths,
        ],
        base=workspace,
    )
    deny_write_paths = deny_paths(
        [
            *sensitive_write_roots(),
            *provider_control_write_paths(
                workspace,
                policy_env,
                extra_roots=tuple(
                    Path(canonical_path(path, base=workspace)) for path in config.extra_write_paths
                ),
            ),
            *toolchain_credential_paths(),
            *config.extra_deny_write_paths,
        ],
        base=workspace,
    )
    domains = allowed_domains(config, provider, api_base)
    unix_sockets = canonical_paths(config.allow_unix_sockets, base=workspace)
    assert_sensitive_path_contract(read_paths, write_paths, unix_sockets)
    assert_credential_read_contract(
        read_paths,
        credential_denies,
        provider_credential_read_exceptions(provider) if provider else [],
    )

    if config.backend == "srt":
        from gobby.agents.agent_workspaces import workspaces_root

        read_paths = config.restrict_workspace_paths(read_paths, write_paths)
        deny_read_paths = list(dict.fromkeys([*deny_read_paths, str(workspaces_root().resolve())]))

    return ResolvedSandboxPaths(
        workspace_path=str(workspace),
        gobby_daemon_port=gobby_daemon_port,
        read_paths=read_paths,
        write_paths=write_paths,
        allow_external_network=config.allow_network,
        deny_read_paths=deny_read_paths,
        deny_write_paths=deny_write_paths,
        allowed_domains=domains,
        denied_domains=list(
            dict.fromkeys(domain.lower() for domain in config.denied_domains if domain)
        ),
        loopback_ports=list(dict.fromkeys((gobby_daemon_port, gobby_websocket_port))),
        allow_unix_sockets=unix_sockets,
        credential_env_vars=(
            [item for item in credential_env_vars(provider, api_base) if item.name in policy_env]
            if provider
            else []
        ),
        provider=provider,
    )


async def _git_metadata_write_paths(workspace: Path) -> list[str]:
    """Return Git metadata dirs that must be writable for commits from a worktree."""
    result = await daemon_git.run(
        ["rev-parse", "--git-dir", "--git-common-dir"],
        cwd=workspace,
        timeout=_GIT_METADATA_TIMEOUT_SECONDS,
    )
    if isinstance(result, GitFailed) and "not a git repository" in result.stderr.lower():
        return []
    if not isinstance(result, GitOk):
        detail = result.stderr.strip() or result.status
        raise RuntimeError(f"Failed to resolve Git metadata for sandbox: {detail}")

    paths: list[str] = []
    for raw_path in result.stdout.splitlines():
        resolved = _resolve_git_metadata_path(workspace, raw_path)
        if resolved and resolved not in paths:
            paths.append(resolved)
    return paths


def _resolve_git_metadata_path(workspace: Path, raw_path: str) -> str | None:
    raw_path = raw_path.strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = workspace / path
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        return str(path)
