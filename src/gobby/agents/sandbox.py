"""Sandbox configuration helpers for spawned CLIs.

This module keeps the provider-specific sandbox contract in one place so the
same rules can be reused by terminal spawns, web-chat backends, and any future
installer/runtime glue that needs to materialize provider settings.
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from gobby.agents.provider_capabilities import provider_supports_sandbox
from gobby.paths import get_gobby_home


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


_DAEMON_OWNED_SANDBOX_MODE: Literal["permissive", "restrictive"] = "permissive"
_DAEMON_SANDBOX_POLICY_VERSION = 1
_WEB_CHAT_POLICY_MISMATCH_MESSAGE = (
    "This chat was created under a different sandbox policy. Continue it in a new chat."
)
logger = logging.getLogger(__name__)
_QWEN_INCLUDE_DIRECTORY_LIMIT = 5
_CLAUDE_LOOPBACK_DOMAINS = ["localhost", "127.0.0.1", "::1"]


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

    return SandboxConfig(
        enabled=bool(getattr(config, "enabled", default_enabled)),
        backend="srt",
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
        "mode": resolved.mode,
        "allow_network": resolved.allow_network,
        "extra_read_paths": resolved.extra_read_paths,
        "extra_write_paths": resolved.extra_write_paths,
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


class SandboxResolver(ABC):
    """
    Abstract base class for CLI-specific sandbox configuration resolution.

    Each CLI (Claude Code, Codex, Qwen) has different mechanisms for
    enabling sandboxing. Subclasses implement the resolve() method to
    convert a SandboxConfig and ResolvedSandboxPaths into CLI-specific
    arguments and environment variables.
    """

    @property
    @abstractmethod
    def cli_name(self) -> str:
        """Return the name of the CLI this resolver handles."""
        ...

    @abstractmethod
    def resolve(
        self, config: SandboxConfig, paths: ResolvedSandboxPaths
    ) -> tuple[list[str], dict[str, str]]:
        """
        Resolve sandbox configuration to CLI-specific args and env vars.

        Args:
            config: The sandbox configuration from the agent definition.
            paths: The resolved paths for the sandbox environment.

        Returns:
            A tuple of (cli_args, env_vars) where:
            - cli_args: List of command-line arguments to pass to the CLI
            - env_vars: Dict of environment variables to set
        """
        ...


class ClaudeSandboxResolver(SandboxResolver):
    """
    Sandbox resolver for Claude Code CLI.

    Claude Code uses --settings with a JSON object containing sandbox config.
    See: https://code.claude.com/docs/en/sandboxing
    """

    @property
    def cli_name(self) -> str:
        return "claude"

    def build_settings(
        self,
        config: SandboxConfig,
        paths: ResolvedSandboxPaths,
    ) -> dict[str, Any]:
        """Return the current documented Claude sandbox settings payload.

        Claude's current documented ``sandbox`` surface is scoped to the Bash
        tool. Context7 did not surface a documented "allow all outbound
        domains" wildcard, so Gobby keeps the sandbox enabled and disables
        unsandboxed fallback without inventing undocumented network semantics.
        """
        if not config.enabled:
            return {}

        sandbox: dict[str, Any] = {
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "excludedCommands": [],
            "network": {
                "allowUnixSockets": [],
                "allowAllUnixSockets": False,
                "allowLocalBinding": config.allow_network,
                "allowedDomains": list(_CLAUDE_LOOPBACK_DOMAINS) if config.allow_network else [],
            },
            "enableWeakerNestedSandbox": False,
        }

        # Grant OS-level write access to writable paths outside the
        # workspace (notably a worktree's Git metadata dirs in the main
        # repo's .git/worktrees/<name> and .git), so sandboxed commits
        # don't fail with EPERM creating index.lock.
        filesystem: dict[str, list[str]] = {
            "denyRead": list(paths.deny_read_paths),
            "denyWrite": list(paths.deny_write_paths),
        }
        allow_write = _external_write_paths(paths)
        if allow_write:
            filesystem["allowWrite"] = allow_write
        sandbox["filesystem"] = filesystem

        return {
            "allowManagedPermissionRulesOnly": True,
            "sandbox": sandbox,
        }

    def resolve(
        self, config: SandboxConfig, paths: ResolvedSandboxPaths
    ) -> tuple[list[str], dict[str, str]]:
        if not config.enabled:
            return ([], {})
        settings = self.build_settings(config, paths)
        return (["--settings", json.dumps(settings, separators=(",", ":"))], {})


class CodexSandboxResolver(SandboxResolver):
    """
    Sandbox resolver for OpenAI Codex CLI.

    Codex uses --sandbox flag with mode (read-only, workspace-write, danger-full-access)
    and --add-dir for additional writable paths.
    See: https://developers.openai.com/codex/cli/reference/
    """

    @property
    def cli_name(self) -> str:
        return "codex"

    @staticmethod
    def sandbox_policy(config: SandboxConfig) -> str:
        """Return Codex CLI's documented sandbox mode string."""
        return "read-only" if config.mode == "restrictive" else "workspace-write"

    @staticmethod
    def thread_sandbox_policy(config: SandboxConfig | None) -> str | None:
        """Return the app-server sandbox policy for web-chat threads."""
        if config is None or not config.enabled:
            return None
        return "read-only" if config.mode == "restrictive" else "workspace-write"

    def resolve(
        self, config: SandboxConfig, paths: ResolvedSandboxPaths
    ) -> tuple[list[str], dict[str, str]]:
        if not config.enabled:
            return ([], {})

        args: list[str] = []

        policy = self.sandbox_policy(config)
        args.extend(["--sandbox", policy])
        if policy == "workspace-write":
            network_access = str(config.allow_network).lower()
            args.extend(["-c", f"sandbox_workspace_write.network_access={network_access}"])

        # Add extra write paths (workspace is implicit in workspace-write mode)
        for path in paths.write_paths:
            if path != paths.workspace_path:
                args.extend(["--add-dir", path])

        return (args, {})


class QwenSandboxResolver(SandboxResolver):
    """Sandbox resolver for Qwen CLI's Seatbelt-backed sandbox contract."""

    @property
    def cli_name(self) -> str:
        return "qwen"

    @staticmethod
    def seatbelt_profile(config: SandboxConfig, paths: ResolvedSandboxPaths) -> str:
        """Return the documented Qwen Seatbelt profile name."""
        mode_prefix = "restrictive" if config.mode == "restrictive" else "permissive"
        network_suffix = "open" if paths.allow_external_network else "proxied"
        return f"{mode_prefix}-{network_suffix}"

    def resolve(
        self, config: SandboxConfig, paths: ResolvedSandboxPaths
    ) -> tuple[list[str], dict[str, str]]:
        if not config.enabled:
            return ([], {})

        args = ["-s"]
        include_dirs = _compact_external_write_paths(
            _external_write_paths(paths),
            limit=_QWEN_INCLUDE_DIRECTORY_LIMIT,
        )
        if len(include_dirs) > _QWEN_INCLUDE_DIRECTORY_LIMIT:
            raise ValueError(
                "Qwen sandbox supports at most "
                f"{_QWEN_INCLUDE_DIRECTORY_LIMIT} external "
                f"--include-directories paths; got {len(include_dirs)}"
            )
        for path in include_dirs:
            args.extend(["--include-directories", path])

        env = {"SEATBELT_PROFILE": self.seatbelt_profile(config, paths)}

        return (args, env)


class GrokSandboxResolver(SandboxResolver):
    """Sandbox resolver for Grok CLI's built-in sandbox profiles."""

    @property
    def cli_name(self) -> str:
        return "grok"

    def resolve(
        self, config: SandboxConfig, paths: ResolvedSandboxPaths
    ) -> tuple[list[str], dict[str, str]]:
        if not config.enabled:
            return ([], {})

        profile = (
            "strict"
            if config.mode == "restrictive" or not paths.allow_external_network
            else "workspace"
        )
        return (["--sandbox", profile], {})


def merge_claude_settings(
    base_settings: dict[str, Any],
    config: SandboxConfig,
    paths: ResolvedSandboxPaths,
) -> dict[str, Any]:
    """Merge Claude sandbox settings into an existing settings payload."""

    def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        merged = dict(left)
        for key, value in right.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = _merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    resolver = ClaudeSandboxResolver()
    overlay = resolver.build_settings(config, paths)
    return _merge(base_settings, overlay)


def preflight_provider_native_settings(
    provider: str,
    settings: dict[str, Any],
    paths: ResolvedSandboxPaths,
    *,
    policy_path: str | None = None,
    policy_hash: str | None = None,
) -> dict[str, Any]:
    """Verify provider-native settings prove the sensitive-path contract."""
    from gobby.agents.sandbox_policy import assert_sensitive_path_contract, sensitive_roots

    if provider != "claude":
        raise ValueError(f"{provider} cannot prove the sensitive-root contract")
    try:
        sandbox = settings["sandbox"]
        filesystem = sandbox["filesystem"]
        denied_read = filesystem["denyRead"]
        denied_write = filesystem["denyWrite"]
        allow_read = filesystem.get("allowRead", [])
        allow_write = filesystem.get("allowWrite", [])
        path_lists = (denied_read, denied_write, allow_read, allow_write)
        if not all(
            isinstance(items, list) and all(isinstance(item, str) for item in items)
            for items in path_lists
        ):
            raise TypeError("provider-native filesystem paths must be string lists")
        if sandbox["enabled"] is not True or sandbox["allowUnsandboxedCommands"] is not False:
            raise ValueError("provider-native sandbox permits unenforced commands")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{provider} emitted an unverifiable sensitive-root policy") from exc

    assert_sensitive_path_contract(paths.read_paths, paths.write_paths, allow_read, allow_write)
    protected = set(sensitive_roots())
    if not protected <= set(denied_read) or not protected <= set(denied_write):
        raise ValueError(f"{provider} emitted an incomplete sensitive-root policy")

    encoded = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    return {
        "backend": "provider-native",
        "enforced": True,
        "policy_hash": policy_hash or hashlib.sha256(encoded).hexdigest(),
        "policy_path": policy_path,
    }


def preflight_provider_native_settings_file(
    *,
    provider: str,
    settings_path: str | None,
    config: SandboxConfig,
    workspace_path: str,
    policy_hash: str | None = None,
) -> dict[str, Any]:
    """Load and verify a provider-native settings file after materialization."""
    if not config.enabled:
        return {"backend": "none", "enforced": False, "policy_hash": policy_hash}
    if not settings_path:
        raise ValueError(f"{provider} did not materialize provider-native settings")
    try:
        settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{provider} emitted unreadable provider-native settings") from exc
    if not isinstance(settings, dict):
        raise ValueError(f"{provider} emitted invalid provider-native settings")
    paths = compute_sandbox_paths(config, workspace_path, provider=provider)
    return preflight_provider_native_settings(
        provider,
        settings,
        paths,
        policy_path=settings_path,
        policy_hash=policy_hash,
    )


async def preflight_provider_native_settings_file_async(
    *,
    provider: str,
    settings_path: str | None,
    config: SandboxConfig,
    workspace_path: str,
    policy_hash: str | None = None,
) -> dict[str, Any]:
    """Verify provider-native settings without blocking the event loop."""
    return await asyncio.to_thread(
        preflight_provider_native_settings_file,
        provider=provider,
        settings_path=settings_path,
        config=config,
        workspace_path=workspace_path,
        policy_hash=policy_hash,
    )


def materialize_claude_settings(
    *,
    base_settings_path: str | Path | None,
    config: SandboxConfig,
    workspace_path: str,
    name: str = "runtime",
) -> str | None:
    """Write a deterministic Claude settings file for SDK-managed sessions."""
    if not config.enabled:
        if base_settings_path:
            return str(Path(base_settings_path))
        return None

    payload: dict[str, Any] = {}
    if base_settings_path:
        source_path = Path(base_settings_path)
        if source_path.exists():
            try:
                raw_payload = json.loads(source_path.read_text(encoding="utf-8"))
                if isinstance(raw_payload, dict):
                    payload = raw_payload
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Failed to read Claude base settings for runtime sandbox overlay",
                    extra={
                        "path": str(source_path),
                        "settings_purpose": "runtime_sandbox_overlay",
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                payload = {}

    resolved_paths = compute_sandbox_paths(config, workspace_path=workspace_path)
    merged = merge_claude_settings(payload, config, resolved_paths)
    encoded = json.dumps(merged, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:12]

    settings_dir = get_gobby_home() / "settings" / "runtime"
    settings_dir.mkdir(parents=True, exist_ok=True)
    target = settings_dir / f"claude-{name}-{digest}.json"
    if not target.exists():
        temp_path = target.with_name(f"{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            with open(temp_path, "wb") as handle:
                handle.write(json.dumps(merged, indent=2).encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
    return str(target)


async def materialize_claude_settings_async(
    *,
    base_settings_path: str | Path | None,
    config: SandboxConfig,
    workspace_path: str,
    name: str = "runtime",
) -> str | None:
    """Materialize Claude settings without blocking the event loop."""
    return await asyncio.to_thread(
        materialize_claude_settings,
        base_settings_path=base_settings_path,
        config=config,
        workspace_path=workspace_path,
        name=name,
    )


def get_sandbox_resolver(cli: str) -> SandboxResolver:
    """
    Factory function to get the appropriate sandbox resolver for a CLI.

    Args:
        cli: The CLI name ("claude", "codex", "grok", or "qwen")

    Returns:
        The appropriate SandboxResolver subclass instance.

    Raises:
        ValueError: If the CLI is not recognized.
    """
    resolvers: dict[str, type[SandboxResolver]] = {
        "claude": ClaudeSandboxResolver,
        "codex": CodexSandboxResolver,
        "grok": GrokSandboxResolver,
        "qwen": QwenSandboxResolver,
    }

    if not provider_supports_sandbox(cli) or cli not in resolvers:
        raise ValueError(f"Unknown CLI: {cli}. Must be one of: {list(resolvers.keys())}")

    return resolvers[cli]()


def compute_sandbox_paths(
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
        default_write_paths,
        deny_paths,
        gobby_read_exceptions,
        mcp_config_read_exceptions,
        provider_read_exceptions,
        provider_write_exceptions,
        sensitive_roots,
        sensitive_write_roots,
        tmux_socket_roots,
        toolchain_credential_paths,
        toolchain_read_roots,
    )

    workspace = Path(canonical_path(workspace_path))
    policy_env = os.environ if env is None else env
    git_paths = _git_metadata_write_paths(workspace)
    write_paths = canonical_paths(
        [
            *default_write_paths(config, workspace),
            *git_paths,
            *(provider_write_exceptions(provider) if provider else []),
        ]
    )
    read_paths = canonical_paths(
        [
            str(workspace),
            *write_paths,
            *gobby_read_exceptions(policy_env),
            *toolchain_read_roots(),
            *mcp_config_read_exceptions(workspace),
            *(
                provider_read_exceptions(
                    provider,
                    policy_env,
                    provider_executable=provider_executable,
                )
                if provider
                else []
            ),
            *config.extra_read_paths,
        ],
        base=workspace,
    )
    deny_read_paths = deny_paths(
        [
            *sensitive_roots(),
            *toolchain_credential_paths(),
            *config.extra_deny_read_paths,
        ],
        base=workspace,
    )
    deny_write_paths = deny_paths(
        [
            *sensitive_write_roots(),
            *toolchain_credential_paths(),
            *config.extra_deny_write_paths,
        ],
        base=workspace,
    )
    domains = allowed_domains(config, provider, api_base)
    assert_sensitive_path_contract(read_paths, write_paths)

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
        allow_unix_sockets=canonical_paths(
            [*config.allow_unix_sockets, *tmux_socket_roots()], base=workspace
        ),
        credential_env_vars=(
            [item for item in credential_env_vars(provider, api_base) if item.name in policy_env]
            if provider
            else []
        ),
        provider=provider,
    )


def _git_metadata_write_paths(workspace: Path) -> list[str]:
    """Return Git metadata dirs that must be writable for commits from a worktree."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "rev-parse",
                "--git-dir",
                "--git-common-dir",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

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


def _normalize_sandbox_path(raw_path: str, *, base: Path | None = None) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return path


def _external_write_paths(paths: ResolvedSandboxPaths) -> list[str]:
    """Return deduped writable paths that sit outside the workspace root.

    These are the paths a CLI sandbox must be told about explicitly (the
    workspace itself is implicitly writable). Notably includes the Git
    metadata dirs of a worktree, whose real location is the main repo's
    ``.git/worktrees/<name>`` and ``.git`` — outside the worktree, so
    commits fail without granting write access here.
    """
    workspace = _normalize_sandbox_path(paths.workspace_path)
    external_paths: list[str] = []
    seen: set[str] = set()

    for raw_path in paths.write_paths:
        resolved_path = _normalize_sandbox_path(raw_path, base=workspace)
        if resolved_path == workspace or resolved_path.is_relative_to(workspace):
            continue
        path_text = str(resolved_path)
        if path_text in seen:
            continue
        seen.add(path_text)
        external_paths.append(path_text)

    return external_paths


def _compact_external_write_paths(paths: list[str], *, limit: int) -> list[str]:
    """Compact related external write paths until they fit provider arg limits."""
    compacted = [Path(path) for path in paths]

    while len(compacted) > limit:
        candidates: dict[Path, set[int]] = {}
        for left_index, left_path in enumerate(compacted):
            for right_index in range(left_index + 1, len(compacted)):
                common_path = _common_write_parent(left_path, compacted[right_index])
                if common_path is None:
                    continue
                candidates.setdefault(common_path, set()).update({left_index, right_index})

        best_parent: Path | None = None
        best_indexes: set[int] = set()
        for parent in candidates:
            covering_indexes = {
                index
                for index, path in enumerate(compacted)
                if path == parent or path.is_relative_to(parent)
            }
            if len(covering_indexes) < 2:
                continue
            key = (len(parent.parts), len(covering_indexes))
            best_key = (len(best_parent.parts), len(best_indexes)) if best_parent else (-1, -1)
            if key > best_key:
                best_parent = parent
                best_indexes = covering_indexes

        if best_parent is None:
            break

        first_index = min(best_indexes)
        next_paths: list[Path] = []
        for index, path in enumerate(compacted):
            if index == first_index:
                next_paths.append(best_parent)
            elif index not in best_indexes:
                next_paths.append(path)
        compacted = next_paths

    return [str(path) for path in compacted]


def _common_write_parent(left_path: Path, right_path: Path) -> Path | None:
    try:
        common_text = os.path.commonpath([str(left_path), str(right_path)])
    except ValueError:
        return None
    common_path = Path(common_text)
    if common_path == common_path.parent:
        return None
    return common_path
