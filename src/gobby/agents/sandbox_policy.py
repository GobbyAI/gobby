"""Canonical policy inputs shared by host sandbox backends."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from gobby.paths import get_gobby_home

if TYPE_CHECKING:
    from gobby.agents.sandbox import SandboxConfig, SandboxCredentialEnv


_PROVIDER_DOMAINS: dict[str, tuple[str, ...]] = {
    "claude": ("api.anthropic.com", "*.anthropic.com"),
    "codex": ("api.openai.com", "*.openai.com", "chatgpt.com", "*.chatgpt.com"),
    "gemini": ("generativelanguage.googleapis.com", "oauth2.googleapis.com"),
    "qwen": ("dashscope.aliyuncs.com", "*.aliyuncs.com"),
    "droid": ("api.factory.ai", "*.factory.ai"),
    "grok": ("api.x.ai", "*.x.ai"),
}

_PROVIDER_AUTH_PATHS: dict[str, tuple[str, ...]] = {
    "claude": ("~/.claude",),
    "codex": ("~/.codex",),
    "gemini": ("~/.gemini", "~/.config/gemini"),
    "qwen": ("~/.qwen",),
    "droid": ("~/.factory",),
    "grok": ("~/.grok",),
}

_PROVIDER_AUTH_READ_ONLY_PATHS: dict[str, tuple[str, ...]] = {
    "claude": ("~/.claude.json", "~/Library/Keychains/login.keychain-db"),
}

_PROVIDER_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "qwen": ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
    "droid": ("FACTORY_API_KEY",),
    "grok": ("XAI_API_KEY",),
}

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
    "proxy.golang.org",
    "sum.golang.org",
    "repo1.maven.org",
)


def canonical_path(raw_path: str | Path, *, base: Path | None = None) -> str:
    """Expand and resolve a policy path without requiring it to exist."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return str(path.resolve(strict=False))


def canonical_paths(paths: list[str], *, base: Path | None = None) -> list[str]:
    """Canonicalize and de-duplicate paths while preserving order."""
    return list(dict.fromkeys(canonical_path(path, base=base) for path in paths))


def sensitive_home_roots() -> list[str]:
    """Return broad home roots denied before narrow read exceptions are applied.

    ~/.gobby is deliberately absent: sandboxed agents get full access to the
    Gobby home (daemon config, hooks, logs, personal project state) — the
    boundary protects the rest of the operator home, not Gobby itself.
    """
    home = Path.home().resolve(strict=False)
    return [str(home)]


def sensitive_write_roots() -> list[str]:
    """Return credential and daemon-state roots that no write grant may override."""
    home = Path.home()
    roots = [
        home / ".ssh",
        home / ".aws",
        home / ".gnupg",
        home / ".kube",
        home / ".config" / "gcloud",
    ]
    return canonical_paths([str(path) for path in roots])


def gobby_write_exceptions() -> list[str]:
    """Return Gobby-owned roots agents must write at runtime.

    Agents own their Gobby surface: hook spools, logs, personal project state,
    and gcode/gwiki state all live under ~/.gobby.

    Shared temp roots are deliberately absent. An agent's scratchpad is the
    per-run directory `srt_runtime` creates under the policy dir and grants
    explicitly. Granting /tmp, /var/tmp, or tempfile.gettempdir() instead
    exposes every concurrent agent's temp state plus the plan-review
    snapshots the spawned `gobby mcp-server` writes, and leaves that
    subprocess writing outside its own sandbox tmp.
    """
    home = Path.home()
    return canonical_paths(
        [
            str(get_gobby_home()),
            # uv's default cache: MCP-server subprocesses run `uv run` with a
            # provider-scrubbed env, so the per-session UV_CACHE_DIR redirect
            # never reaches them and uv falls back to these roots.
            str(home / ".cache" / "uv"),
            str(home / "Library" / "Caches" / "uv"),
        ]
    )


def gobby_read_exceptions(env: Mapping[str, str]) -> list[str]:
    """Return exact machine, hook, binary, and prompt resources needed by agents."""
    # The whole Gobby home: bootstrap.yaml is the root of trust for daemon
    # and hub discovery (ghook, `gobby mcp-server`, gcode, and gwiki re-read
    # it per invocation), and agents read hook config, binaries, the local
    # CLI token, and personal project state from here.
    paths = [get_gobby_home()]
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
    tmux_tmpdir = os.environ.get("TMUX_TMPDIR", "/tmp")  # noqa: S108
    return canonical_paths([str(Path(tmux_tmpdir) / f"tmux-{os.getuid()}")])


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


def package_cache_paths() -> list[str]:
    """Return capability-scoped package caches used by common managed agents."""
    return canonical_paths(
        ["~/.cache/uv", "~/.cache/pip", "~/.npm", "~/.cargo/registry", "~/.cargo/git"]
    )


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
    """Resolve writable workspace, operator, and package-cache roots."""
    paths = [str(workspace), *config.extra_write_paths]
    if config.allow_package_registries:
        paths.extend(package_cache_paths())
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
