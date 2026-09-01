"""Canonical policy inputs shared by host sandbox backends."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from gobby.agents.credential_inventory import denied_ambient_keys
from gobby.config.tmux import socket_root
from gobby.paths import get_gobby_home

if TYPE_CHECKING:
    from gobby.agents.sandbox import SandboxConfig, SandboxCredentialEnv


_PROVIDER_DOMAINS: dict[str, tuple[str, ...]] = {
    "claude": ("api.anthropic.com", "*.anthropic.com"),
    "codex": ("api.openai.com", "*.openai.com", "chatgpt.com", "*.chatgpt.com"),
    "gemini": ("generativelanguage.googleapis.com", "oauth2.googleapis.com"),
    "qwen": ("dashscope.aliyuncs.com", "*.aliyuncs.com"),
    "droid": ("api.factory.ai", "*.factory.ai"),
    "grok": ("api.x.ai", "*.x.ai", "grok.com", "*.grok.com"),
    "agy": (
        "daily-cloudcode-pa.googleapis.com",
        "oauth2.googleapis.com",
        "accounts.google.com",
        "play.googleapis.com",
        "playwright.azureedge.net",
        "playwright-akamai.azureedge.net",
        "playwright-verizon.azureedge.net",
        "googleusercontent.com",
    ),
}

_PROVIDER_AUTH_PATHS: dict[str, tuple[str, ...]] = {
    "claude": ("~/.claude",),
    "codex": ("~/.codex",),
    "gemini": ("~/.gemini", "~/.config/gemini"),
    "qwen": ("~/.qwen",),
    "droid": ("~/.factory",),
    "grok": ("~/.grok",),
    "agy": (
        "~/.gemini/antigravity-cli",
        "~/Library/Caches/ms-playwright-go",
    ),
}

_PROVIDER_AUTH_READ_ONLY_PATHS: dict[str, tuple[str, ...]] = {
    "claude": ("~/.claude.json", "~/Library/Keychains/login.keychain-db"),
    "agy": (
        "~/.gemini/config/projects",
        "~/Library/Keychains/login.keychain-db",
    ),
}

_PROVIDER_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "qwen": ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
    "droid": ("FACTORY_API_KEY",),
    "grok": ("XAI_API_KEY",),
    # AGY never accepts env auth; the tmux spawner strips these same keys.
    "agy": denied_ambient_keys("agy"),
}

_RUN_CACHE_ENV_VARS = (
    "UV_CACHE_DIR",
    "CARGO_HOME",
    "GOCACHE",
    "GOMODCACHE",
    "npm_config_cache",
    "YARN_CACHE_FOLDER",
    "PNPM_HOME",
    "PIP_CACHE_DIR",
    "GRADLE_USER_HOME",
    "COURSIER_CACHE",
    "NUGET_PACKAGES",
    "COMPOSER_CACHE_DIR",
    "PUB_CACHE",
    "GEM_HOME",
    "BUNDLE_PATH",
    "HEX_HOME",
    "MIX_HOME",
    "XDG_CACHE_HOME",
)

SRT_SETTINGS_RELATIVE_PATH = Path("assets") / "settings.json"
SRT_VIOLATIONS_RELATIVE_PATH = Path("logs") / "violations.jsonl"


@dataclass(frozen=True)
class SandboxRunPaths:
    root: Path
    assets: Path
    tmp: Path
    hooks: Path
    logs: Path
    cache: Path

    @property
    def writable(self) -> tuple[Path, Path, Path, Path]:
        return (self.tmp, self.hooks, self.logs, self.cache)

    def environment(self, provider: str) -> dict[str, str]:
        values = {
            name: str(self.cache / name.replace("_", "-").lower()) for name in _RUN_CACHE_ENV_VARS
        }
        values["CLAUDE_CODE_TMPDIR" if provider == "claude" else "TMPDIR"] = str(self.tmp)
        values["GOBBY_LOG_DIR"] = str(self.logs)
        return values


_GIT_DOMAINS = (
    "github.com",
    "*.github.com",
    "gitlab.com",
    "*.gitlab.com",
    "bitbucket.org",
    "*.bitbucket.org",
)

_PACKAGE_REGISTRY_DOMAINS = (
    "registry.npmjs.org",
    "*.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    "static.rust-lang.org",
    "proxy.golang.org",
    "sum.golang.org",
    "repo1.maven.org",
    "plugins.gradle.org",
    "rubygems.org",
    "api.nuget.org",
    "repo.packagist.org",
    "pub.dev",
    "repo.hex.pm",
    "luarocks.org",
)

# Toolchains installed under $HOME. sensitive_roots() denies five specific
# Gobby-owned paths; these tables separately grant the compiler, SDK, package,
# and cache roots needed by sandboxed agents. System toolchains outside $HOME
# do not need $HOME-relative entries here.
#
# Coverage tracks the languages gcode indexes
# (crates/gcode/src/index/languages.rs). Entries are $HOME-relative and joined
# to Path.home() at resolution time.

# Compilers, interpreters, and SDKs. Read-only: agents run these, never patch
# them, and a writable toolchain would let one run's build poison the next.
_TOOLCHAIN_READ_ROOTS: tuple[str, ...] = (
    ".rustup",  # rust
    ".pyenv",  # python
    ".nvm",  # javascript, typescript
    ".fnm",
    ".volta",
    ".bun",
    ".deno",
    ".sdkman",  # java, kotlin, scala
    ".jenv",
    ".konan",
    "Library/Java",
    ".rbenv",  # ruby
    ".rvm",
    ".dotnet",  # csharp
    ".phpenv",  # php
    "flutter",  # dart
    ".swiftly",  # swift
    "sdk",  # go, via `go install golang.org/dl/...`
    ".asdf",  # multi-language version managers
    ".mise",
    ".local/share/mise",
    ".local/bin",  # shared bin dir the above symlink into
)

# Installed packages, package metadata, and shared build caches are read-only.
# Credential-bearing parents are split into safe children because SRT v0.0.66
# lets an allowRead ancestor override a nested denyRead entry.
_TOOLCHAIN_SHARED_READ_ROOTS: tuple[str, ...] = (
    ".cargo/bin",  # rust
    ".cargo/registry",
    ".cargo/git",
    ".cargo/.package-cache",
    ".cargo/config",
    ".cargo/config.toml",
    ".cache/uv",  # python
    "Library/Caches/uv",
    ".cache/pip",
    "Library/Caches/pip",
    ".npm/_cacache",  # javascript, typescript
    ".npm/_npx",
    ".npm/_logs",
    ".cache/npm",
    ".pnpm-store",
    "Library/pnpm",
    ".yarn",
    ".cache/yarn",
    ".config/yarn",
    "go",  # go: GOPATH module cache
    ".cache/go-build",
    "Library/Caches/go-build",
    ".gradle/caches",  # java, kotlin, scala
    ".gradle/wrapper",
    ".gradle/jdks",
    ".gradle/daemon",
    ".gradle/native",
    ".gradle/init.d",
    ".gradle/init.gradle",
    ".gradle/init.gradle.kts",
    ".m2/repository",
    ".m2/wrapper",
    ".m2/toolchains.xml",
    ".ivy2/cache",
    ".ivy2/jars",
    ".ivy2/local",
    ".sbt/boot",
    ".sbt/1.0",
    ".sbt/preloaded",
    ".sbt/repositories",
    ".cache/coursier",
    "Library/Caches/Coursier",
    ".gem/ruby",  # ruby
    ".gem/specs",
    ".gem/cache",
    ".gem/extensions",
    ".bundle/cache",
    ".bundle/plugin",
    ".nuget/packages",  # csharp
    ".nuget/plugins",
    ".nuget/fallbackpackages",
    ".local/share/NuGet",
    ".composer/cache",  # php
    ".composer/vendor",
    ".composer/config.json",
    ".composer/composer.json",
    ".config/composer/cache",
    ".config/composer/vendor",
    ".config/composer/config.json",
    ".config/composer/composer.json",
    ".cache/composer",
    ".pub-cache/hosted",  # dart
    ".pub-cache/git",
    ".pub-cache/bin",
    ".hex/packages",  # elixir
    ".hex/cache.ets",
    ".mix",
    ".luarocks",  # lua
    ".cache/luarocks",
    ".cache/ccache",  # c, cpp, objc
    "Library/Caches/ccache",
    ".cache/clangd",
    ".swiftpm",  # swift
    "Library/Caches/org.swift.swiftpm",
)

# Registry credentials colocated with toolchain state. Their parents are
# excluded from the granular read grants above, and exact read/write denies
# preserve that boundary for files created after policy generation.
_TOOLCHAIN_CREDENTIAL_PATHS: tuple[str, ...] = (
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".npm/_auth",
    ".gradle/gradle.properties",
    ".m2/settings.xml",
    ".m2/settings-security.xml",
    ".gem/credentials",
    ".bundle/config",
    ".sbt/.credentials",
    ".ivy2/.credentials",
    ".nuget/NuGet/NuGet.Config",
    ".composer/auth.json",
    ".config/composer/auth.json",
    ".pub-cache/credentials.json",
    ".hex/hex.config",
)


def _absolute_path(raw_path: str | Path, *, base: Path | None = None) -> str:
    """Expand and absolutize a policy path without resolving symlinks."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return os.path.abspath(path)


def canonical_path(raw_path: str | Path, *, base: Path | None = None) -> str:
    """Expand and resolve a policy path without requiring it to exist."""
    path = Path(_absolute_path(raw_path, base=base))
    return str(path.resolve(strict=False))


def canonical_paths(paths: list[str], *, base: Path | None = None) -> list[str]:
    """Canonicalize and de-duplicate paths while preserving order."""
    return list(dict.fromkeys(canonical_path(path, base=base) for path in paths))


def deny_paths(paths: list[str], *, base: Path | None = None) -> list[str]:
    """Emit literal and symlink-resolved deny paths while preserving order."""
    variants = (
        variant
        for path in paths
        for variant in (
            _absolute_path(path, base=base),
            canonical_path(path, base=base),
        )
    )
    return list(dict.fromkeys(variants))


def sensitive_roots() -> list[str]:
    """Return Gobby roots excluded from every managed allow surface."""
    gobby_home = get_gobby_home()
    return deny_paths(
        [
            str(gobby_home / "bootstrap.yaml"),
            str(gobby_home / ".secret_kek"),
            str(gobby_home / "local_cli_token"),
            str(gobby_home / "gcode-runtime"),
            str(gobby_home / "tools" / "srt"),
        ]
    )


def assert_sensitive_path_contract(*allow_lists: list[str]) -> None:
    """Reject an allow entry that contains a sensitive root."""
    protected = [Path(path) for path in sensitive_roots()]
    for allowed_text in (item for paths in allow_lists for item in paths):
        allowed = Path(allowed_text).resolve(strict=False)
        for sensitive in protected:
            if sensitive == allowed or sensitive.is_relative_to(allowed):
                raise ValueError(f"sandbox allow path contains sensitive root: {allowed}")


def sensitive_write_roots() -> list[str]:
    """Return credential and daemon-state roots that no write grant may override."""
    home = Path.home()
    roots = [
        home / ".ssh",
        home / ".aws",
        home / ".gnupg",
        home / ".kube",
        home / ".config" / "gcloud",
        *map(Path, sensitive_roots()),
    ]
    return deny_paths([str(path) for path in roots])


def gobby_read_exceptions(env: Mapping[str, str]) -> list[str]:
    """Return Gobby state, runtime, and prompt resources needed by agents."""
    # The whole Gobby home: bootstrap.yaml is the root of trust for daemon
    # and hub discovery (ghook, `gobby mcp-server`, gcode, and gwiki re-read
    # it per invocation), and agents need machine_id, logs, binaries, the local
    # operator credential, hook config, and personal project state from here.
    # The credential remains write-denied by sensitive_write_roots().
    home = Path.home()
    paths = [
        # Codex scrubs the MCP subprocess environment, so uv falls back to its
        # default cache roots. Shared cache contents remain readable while
        # writable cache state is routed through a per-run extra_write_path.
        home / ".cache" / "uv",
        home / "Library" / "Caches" / "uv",
    ]
    runtime_home = env.get("GOBBY_CODE_INDEX_RUNTIME_HOME")
    if runtime_home:
        paths.append(Path(runtime_home))
    managed_bootstrap = env.get("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
    if managed_bootstrap:
        paths.append(Path(managed_bootstrap))
    prompt_file = env.get("GOBBY_PROMPT_FILE")
    if prompt_file:
        paths.append(Path(prompt_file))
    # Workspace tooling: `uv run` backs both agent shell commands and the
    # spawned `gobby mcp-server` stdio process. The uv binary and its managed
    # interpreters (venv symlink targets) live outside the workspace and stay
    # read-only.
    uv_binary = shutil.which("uv", path=env.get("PATH"))
    if uv_binary:
        uv_path = Path(uv_binary).expanduser().absolute()
        paths.extend([uv_path, uv_path.resolve(strict=False)])
    paths.append(Path("~/.local/share/uv").expanduser())
    return canonical_paths([str(path) for path in paths])


def gcode_runtime_write_exceptions(env: Mapping[str, str]) -> list[str]:
    """Allow renewal writes only inside this run's generated gcode home."""
    runtime_home = env.get("GOBBY_CODE_INDEX_RUNTIME_HOME")
    return canonical_paths([runtime_home]) if runtime_home else []


def mcp_config_read_exceptions(workspace: Path) -> list[str]:
    """Return project roots the workspace's own MCP servers resolve from.

    Isolated agents get a generated ``.mcp.json`` whose gobby entry runs
    ``uv run --project <main repo> gobby mcp-server`` so the proxy executes the
    main repo's Gobby code (``_patch_mcp_config_for_isolation``). The sandbox
    denies the operator home, so without a matching read grant ``uv`` cannot
    open the main repo's ``pyproject.toml``, the MCP subprocess dies, and the
    agent starts with no proxy tools at all (#19097). Read access is enough:
    the isolated workspace stays the only writable source tree.
    """
    try:
        config = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        return []

    server = servers.get("gobby")
    args = server.get("args") if isinstance(server, dict) else None
    if not isinstance(args, list):
        return []
    roots = [
        arg
        for arg in args
        if isinstance(arg, str) and Path(arg).is_absolute() and Path(arg).is_dir()
    ]
    return canonical_paths(roots)


def provider_read_exceptions(
    provider: str,
    env: Mapping[str, str],
    *,
    provider_executable: str | None = None,
) -> list[str]:
    """Return exact auth/config and executable roots required by one provider."""
    paths = list(_PROVIDER_AUTH_PATHS.get(provider, ()))
    paths.extend(_PROVIDER_AUTH_READ_ONLY_PATHS.get(provider, ()))
    paths.extend(("~/.gitconfig", "~/.config/git"))

    search_path = env.get("PATH")
    for executable in (
        provider_executable or shutil.which(provider, path=search_path),
        shutil.which("node", path=search_path),
    ):
        if not executable:
            continue
        executable_path = Path(executable).expanduser().absolute()
        paths.append(str(executable_path))
        target = executable_path.resolve(strict=False)
        paths.append(str(target))
        package_root = _nearest_package_root(target)
        if package_root is not None:
            paths.append(str(package_root))
    return canonical_paths(paths)


def tmux_socket_roots() -> list[str]:
    """Return the tmux server socket directory for agent coordination IPC.

    Gobby runs agents inside a dedicated tmux server; in-sandbox tools that
    coordinate through it (statusline, pane messaging) connect to sockets
    under this per-uid directory.
    """
    if not hasattr(os, "getuid"):
        return []
    return canonical_paths([socket_root()])


def provider_write_exceptions(provider: str) -> list[str]:
    """Return provider-owned state roots the CLI must write at runtime.

    Every hosted CLI persists runtime state inside its auth root (Codex keeps
    its sqlite state DB and session rollouts in ~/.codex, Droid writes
    ~/.factory, token refresh rewrites auth files). Read-only auth roots make
    the provider exit at bootstrap. Sensitive roots stay protected because
    seatbelt deny rules (sensitive_write_roots) take precedence over allows.
    """
    return canonical_paths(list(_PROVIDER_AUTH_PATHS.get(provider, ())))


def _nearest_package_root(executable: Path) -> Path | None:
    home = Path.home().resolve(strict=False)
    for parent in executable.parents[:2]:
        if parent == home:
            break
        if (parent / "package.json").is_file():
            return parent
    return None


def _existing_home_roots(relative_roots: tuple[str, ...]) -> list[str]:
    """Resolve $HOME-relative roots, dropping the ones absent on this machine.

    Existence filtering keeps the emitted policy tight: a machine with no Go
    toolchain never gets a Go grant.
    """
    home = Path.home()
    return canonical_paths([str(home / root) for root in relative_roots if (home / root).exists()])


def toolchain_read_roots() -> list[str]:
    """Return read-only compiler, SDK, installed-package, and shared-cache roots."""
    return _existing_home_roots((*_TOOLCHAIN_READ_ROOTS, *_TOOLCHAIN_SHARED_READ_ROOTS))


def toolchain_credential_paths() -> list[str]:
    """Return registry credentials nested inside granted toolchain roots."""
    home = Path.home()
    return canonical_paths([str(home / path) for path in _TOOLCHAIN_CREDENTIAL_PATHS])


def allowed_domains(
    config: SandboxConfig,
    provider: str | None,
    api_base: str | None,
) -> list[str]:
    """Resolve provider, local endpoint, Git, registry, and operator domain grants."""
    provider_domains = _PROVIDER_DOMAINS.get(provider, ()) if provider else ()
    domains = [*provider_domains, *config.allowed_domains]
    if api_base:
        parsed = urlparse(api_base)
        if parsed.hostname:
            domains.append(parsed.hostname)
    if config.allow_git_network:
        domains.extend(_GIT_DOMAINS)
    if config.allow_package_registries:
        domains.extend(_PACKAGE_REGISTRY_DOMAINS)
    domains.extend(("localhost", "127.0.0.1"))
    return list(dict.fromkeys(domain.lower() for domain in domains if domain))


def credential_env_vars(provider: str, api_base: str | None) -> list[SandboxCredentialEnv]:
    """Mask provider tokens in-process and inject them only at provider API hosts."""
    from gobby.agents.sandbox import SandboxCredentialEnv

    inject_hosts = list(_PROVIDER_DOMAINS.get(provider, ()))
    if api_base:
        parsed = urlparse(api_base)
        if parsed.hostname:
            inject_hosts.append(parsed.hostname.lower())
    inject_hosts = list(dict.fromkeys(inject_hosts))
    return [
        SandboxCredentialEnv(name=name, mode="mask", inject_hosts=inject_hosts)
        for name in _PROVIDER_CREDENTIAL_ENV.get(provider, ())
    ]


def default_write_paths(config: SandboxConfig, workspace: Path) -> list[str]:
    """Resolve the workspace and explicit per-run writable roots.

    Toolchain caches that require mutation are provisioned per run and arrive
    through ``extra_write_paths``. Shared caches stay read-only.
    """
    paths = [str(workspace), *config.extra_write_paths]
    return canonical_paths(paths, base=workspace)


def secure_policy_directory(run_id: str) -> Path:
    """Return the trusted per-run directory used for policy and violation files."""
    safe_run_id = "".join(char for char in run_id if char.isalnum() or char in "-_")
    if not safe_run_id:
        raise ValueError("agent run ID has no safe policy-directory characters")
    directory = get_gobby_home() / "run" / "sandbox" / safe_run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def managed_execution_root() -> Path:
    """Return the daemon-owned root for credential-scoped executions."""
    return get_gobby_home() / "runtime" / "managed-executions"


def srt_mux_tmpdir() -> Path:
    """Return the short shared directory for SRT runner-internal unix sockets.

    sandbox-runtime allocates its mux/TLS sockets under ``os.tmpdir()``; the
    per-run managed-execution TMPDIR is too deep for ``sun_path`` (104 bytes on
    macOS), so the runner process gets this short root via GOBBY_SRT_TMPDIR
    while the provider child keeps the policy-allowed per-run TMPDIR.
    """
    directory = get_gobby_home() / "runtime" / "srt-sock"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def prepare_sandbox_run_paths(run_id: str, env: Mapping[str, str]) -> SandboxRunPaths:
    """Materialize one daemon-owned run root with four writable siblings."""
    managed_root = managed_execution_root()
    bootstrap = env.get("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
    bootstrap_path = Path(bootstrap).resolve(strict=False) if bootstrap else None
    if bootstrap_path is not None and bootstrap_path.parent.is_relative_to(
        managed_root.resolve(strict=False)
    ):
        root = bootstrap_path.parent
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    else:
        root = secure_policy_directory(run_id)
    paths = SandboxRunPaths(
        root=root,
        assets=root / "assets",
        tmp=root / "tmp",
        hooks=root / "hooks",
        logs=root / "logs",
        cache=root / "cache",
    )
    for path in (paths.assets, *paths.writable):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    for cache_path in paths.environment("unknown").values():
        candidate = Path(cache_path)
        if candidate.is_relative_to(paths.cache):
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
    return paths


def previous_run_write_paths(env: Mapping[str, str]) -> set[str]:
    """Return superseded shared cache grants replaced by a run root.

    The hook inbox remains shared: ghook's durable transport always resolves
    ``$GOBBY_HOME/hooks/inbox`` and must be able to enqueue and unlink there.
    """
    return {canonical_path(value) for name in _RUN_CACHE_ENV_VARS if (value := env.get(name))}
