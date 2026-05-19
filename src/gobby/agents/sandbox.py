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
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field


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
        allow_network: Whether to allow network access (except localhost:60887
            which is always allowed for Gobby daemon communication).
        extra_read_paths: Additional paths to allow read access.
        extra_write_paths: Additional paths to allow write access
            (worktree paths are always allowed).
    """

    enabled: bool = False
    mode: Literal["permissive", "restrictive"] = "permissive"
    allow_network: bool = True
    extra_read_paths: list[str] = Field(default_factory=list)
    extra_write_paths: list[str] = Field(default_factory=list)


_DAEMON_OWNED_SANDBOX_MODE: Literal["permissive", "restrictive"] = "permissive"
_DAEMON_OWNED_ALLOW_NETWORK = True
_DAEMON_SANDBOX_POLICY_VERSION = 1
_WEB_CHAT_POLICY_MISMATCH_MESSAGE = (
    "This chat was created under a different sandbox policy. Continue it in a new chat."
)
logger = logging.getLogger(__name__)
_GEMINI_INCLUDE_DIRECTORY_LIMIT = 5


def coerce_sandbox_config(config: Any | None) -> SandboxConfig | None:
    """Normalize a config-like object into SandboxConfig."""
    if config is None:
        return None
    if isinstance(config, SandboxConfig):
        return config.model_copy(deep=True)
    if isinstance(config, dict):
        return SandboxConfig(**config)

    raw_mode = str(getattr(config, "mode", "permissive"))
    mode = cast(
        Literal["permissive", "restrictive"],
        raw_mode if raw_mode in {"permissive", "restrictive"} else "permissive",
    )

    return SandboxConfig(
        enabled=bool(getattr(config, "enabled", False)),
        mode=mode,
        allow_network=bool(getattr(config, "allow_network", True)),
        extra_read_paths=list(getattr(config, "extra_read_paths", []) or []),
        extra_write_paths=list(getattr(config, "extra_write_paths", []) or []),
    )


def daemon_owned_sandbox_config(
    config: Any | None,
    *,
    default_enabled: bool = True,
) -> SandboxConfig:
    """Resolve daemon-owned sandbox config into the internal runtime model."""
    if config is None:
        return SandboxConfig(
            enabled=default_enabled,
            mode=_DAEMON_OWNED_SANDBOX_MODE,
            allow_network=_DAEMON_OWNED_ALLOW_NETWORK,
        )

    return SandboxConfig(
        enabled=bool(getattr(config, "enabled", default_enabled)),
        mode=_DAEMON_OWNED_SANDBOX_MODE,
        allow_network=_DAEMON_OWNED_ALLOW_NETWORK,
        extra_read_paths=list(getattr(config, "extra_read_paths", []) or []),
        extra_write_paths=list(getattr(config, "extra_write_paths", []) or []),
    )


def web_chat_sandbox_config(daemon_config: Any | None) -> SandboxConfig:
    """Return the daemon-owned web-chat sandbox config."""
    raw_config = getattr(daemon_config, "web_chat_sandbox", None) if daemon_config else None
    return daemon_owned_sandbox_config(raw_config, default_enabled=True)


def agent_sandbox_config(daemon_config: Any | None) -> SandboxConfig:
    """Return the daemon-owned spawned-agent sandbox config."""
    raw_config = getattr(daemon_config, "agent_sandbox", None) if daemon_config else None
    return daemon_owned_sandbox_config(raw_config, default_enabled=True)


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


class SandboxResolver(ABC):
    """
    Abstract base class for CLI-specific sandbox configuration resolution.

    Each CLI (Claude Code, Codex, Gemini) has different mechanisms for
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

        return {
            "allowManagedPermissionRulesOnly": True,
            "sandbox": {
                "enabled": True,
                "autoAllowBashIfSandboxed": False,
                "allowUnsandboxedCommands": False,
                "excludedCommands": [],
                "network": {
                    "allowUnixSockets": [],
                    "allowAllUnixSockets": False,
                    "allowLocalBinding": False,
                    "allowedDomains": [],
                },
                "enableWeakerNestedSandbox": False,
            },
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

        args.extend(["--sandbox", self.sandbox_policy(config)])

        # Add extra write paths (workspace is implicit in workspace-write mode)
        for path in paths.write_paths:
            if path != paths.workspace_path:
                args.extend(["--add-dir", path])

        return (args, {})


class GeminiSandboxResolver(SandboxResolver):
    """
    Sandbox resolver for Google Gemini CLI.

    Gemini uses -s/--sandbox flag and SEATBELT_PROFILE env var for macOS.
    See: https://geminicli.com/docs/cli/sandbox/
    """

    @property
    def cli_name(self) -> str:
        return "gemini"

    @staticmethod
    def seatbelt_profile(config: SandboxConfig, paths: ResolvedSandboxPaths) -> str:
        """Return the documented Gemini/Qwen Seatbelt profile name."""
        mode_prefix = "restrictive" if config.mode == "restrictive" else "permissive"
        network_suffix = "open" if paths.allow_external_network else "proxied"
        return f"{mode_prefix}-{network_suffix}"

    @staticmethod
    def _external_write_paths(paths: ResolvedSandboxPaths) -> list[str]:
        """Return deduped writable paths that sit outside the workspace root."""
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

    def resolve(
        self, config: SandboxConfig, paths: ResolvedSandboxPaths
    ) -> tuple[list[str], dict[str, str]]:
        if not config.enabled:
            return ([], {})

        args = ["-s"]
        include_dirs = self._external_write_paths(paths)
        if len(include_dirs) > _GEMINI_INCLUDE_DIRECTORY_LIMIT:
            raise ValueError(
                "Gemini/Qwen sandbox supports at most "
                f"{_GEMINI_INCLUDE_DIRECTORY_LIMIT} external "
                f"--include-directories paths; got {len(include_dirs)}"
            )
        for path in include_dirs:
            args.extend(["--include-directories", path])

        env = {"SEATBELT_PROFILE": self.seatbelt_profile(config, paths)}

        return (args, env)


class QwenSandboxResolver(GeminiSandboxResolver):
    """Qwen currently follows the same sandbox contract as Gemini."""

    @property
    def cli_name(self) -> str:
        return "qwen"


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

    settings_dir = Path.home() / ".gobby" / "settings" / "runtime"
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
        cli: The CLI name ("claude", "codex", or "gemini")

    Returns:
        The appropriate SandboxResolver subclass instance.

    Raises:
        ValueError: If the CLI is not recognized.
    """
    resolvers: dict[str, type[SandboxResolver]] = {
        "claude": ClaudeSandboxResolver,
        "codex": CodexSandboxResolver,
        "gemini": GeminiSandboxResolver,
        "qwen": QwenSandboxResolver,
    }

    if cli not in resolvers:
        raise ValueError(f"Unknown CLI: {cli}. Must be one of: {list(resolvers.keys())}")

    return resolvers[cli]()


def compute_sandbox_paths(
    config: SandboxConfig,
    workspace_path: str,
    gobby_daemon_port: int = 60887,
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
    workspace = Path(workspace_path).expanduser()

    # Start with workspace in write paths.
    write_paths = [workspace_path]

    for path in _git_metadata_write_paths(workspace):
        if path not in write_paths:
            write_paths.append(path)

    # Add extra write paths
    for path in config.extra_write_paths:
        if path not in write_paths:
            write_paths.append(path)

    # Collect read paths - always include ~/.gobby/ for machine_id access
    gobby_home = str(Path("~/.gobby").expanduser())
    read_paths = [gobby_home] + list(config.extra_read_paths)

    return ResolvedSandboxPaths(
        workspace_path=workspace_path,
        gobby_daemon_port=gobby_daemon_port,
        read_paths=read_paths,
        write_paths=write_paths,
        allow_external_network=config.allow_network,
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
    except OSError:
        return str(path)


def _normalize_sandbox_path(raw_path: str, *, base: Path | None = None) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    try:
        return path.resolve(strict=False)
    except OSError:
        return path
