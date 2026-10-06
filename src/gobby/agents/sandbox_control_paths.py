"""Host configuration and credential paths protected from managed processes.

Provider runtime grants remain separate: denyWrite wins over those grants, while
allowRead wins over denyRead and must be checked when assembling a policy.
"""

from collections.abc import Mapping
from glob import has_magic
from pathlib import Path

# Include fallback settings and directories accepting arbitrary hook files, not
# only the particular hook file written by Gobby's installer.
GROK_RUNTIME_DIRECTORIES = ("sessions", "memory", "logs", "crash", "trace-exports", "worktrees")
GROK_CONTROL_FILES = (
    "hooks",
    "hooks-paths",
    "config.toml",
    "managed_config.toml",
    "requirements.toml",
    "trusted_folders.toml",
    "trusted-hook-projects",
    "trusted-plugins",
    "sandbox.toml",
    "bin",
)

_PROVIDER_CONTROLS: dict[str, tuple[str, ...]] = {
    ".claude": (
        "settings.json",
        "settings.local.json",
        "plugins/cache",
        "plugins/marketplaces",
        "plugins/synced",
        "plugins/installed_plugins.json",
        "plugins/known_marketplaces.json",
    ),
    ".codex": ("hooks.json", "config.toml", "plugins/cache"),
    ".qwen": ("settings.json", "trustedFolders.json", "extensions", "extension-store"),
    ".factory": (
        "hooks.json",
        "settings.json",
        "settings.local.json",
        "plugins/cache",
        "plugins/marketplaces",
        "plugins/installed_plugins.json",
        "plugins/known_marketplaces.json",
    ),
    ".grok": GROK_CONTROL_FILES,
    ".cursor": ("hooks.json",),
    ".gemini": ("settings.json", "trustedFolders.json", "config/hooks.json", "extensions"),
    ".config/gemini": ("settings.json", "trustedFolders.json"),
    ".agents/plugins": ("marketplace.json",),
    ".claude-plugin": ("marketplace.json",),
}

_CONFIG_HOME_ENV = {
    "CLAUDE_CONFIG_DIR": ".claude",
    "CODEX_HOME": ".codex",
    "QWEN_HOME": ".qwen",
}


def provider_control_write_paths(
    workspace: Path, env: Mapping[str, str], *, extra_roots: tuple[Path, ...] = ()
) -> list[str]:
    """Protect host and inherited project control files, including alternate homes."""
    paths = [str(Path.home() / ".claude.json")]
    control_roots: list[Path] = []
    for extra in extra_roots:
        # File and glob grants are not configuration directories. Inventing
        # controls below them makes SRT pin the file itself against rename.
        for index, part in enumerate(extra.parts):
            if has_magic(part):
                extra = Path(*extra.parts[:index])
                break
        if extra.is_file():
            extra = extra.parent
        control_roots.extend((extra, *extra.parents))
    # CLIs can inherit project settings from ancestors. Also protect absent files
    # so a managed process cannot install a new hook for a later host session.
    roots = dict.fromkeys(
        (
            Path.home(),
            workspace,
            *workspace.parents,
            *control_roots,
        )
    )
    for root in roots:
        for directory, controls in _PROVIDER_CONTROLS.items():
            paths.extend(str(root / directory / name) for name in controls)
    for variable, directory in _CONFIG_HOME_ENV.items():
        if configured := env.get(variable):
            root = Path(configured).expanduser()
            paths.extend(str(root / name) for name in _PROVIDER_CONTROLS[directory])
            if variable == "CLAUDE_CONFIG_DIR":
                paths.append(str(root / ".claude.json"))
    if configured := env.get("GOBBY_DROID_HOOKS_FILE"):
        paths.append(configured)
    if configured := env.get("GOBBY_HOOKS_DIR"):
        paths.append(str(Path(configured).expanduser() / "hooks.json"))
    return list(dict.fromkeys(paths))


def user_credential_read_paths() -> list[str]:
    """Credential stores are denied as parents; required children are explicit."""
    return [
        "~/.ssh",
        "~/.aws",
        "~/.gnupg",
        "~/.kube",
        "~/.config/gcloud",
        "~/.config/gh",
        "~/.netrc",
        "~/.docker/config.json",
        "~/Library/Keychains",
    ]


def assert_credential_read_contract(
    read_paths: list[str], credential_roots: list[str], required_paths: list[str]
) -> None:
    """Allow only declared necessities to override a credential-store denial."""
    required = [Path(path) for path in required_paths]
    home = Path.home()
    # Resolve the directory, not the final child: a symlink named known_hosts
    # pointing to a private key must not turn that key into a safe exception.
    safe_user_reads = {(home / ".ssh").resolve() / "known_hosts"}
    keychain = (home / "Library/Keychains").resolve() / "login.keychain-db"
    if keychain in required:
        safe_user_reads.add(keychain)
    user_roots = [Path(path).expanduser().resolve() for path in user_credential_read_paths()]
    for raw_allowed in read_paths:
        allowed = Path(raw_allowed)
        for root in user_roots:
            if (allowed.is_relative_to(root) or root.is_relative_to(allowed)) and (
                allowed not in safe_user_reads
            ):
                raise ValueError(f"sandbox read grant shadows credential deny: {allowed}")
        for raw_root in credential_roots:
            root = Path(raw_root)
            if (
                (allowed.is_relative_to(root) or root.is_relative_to(allowed))
                and allowed not in safe_user_reads
                and not any(allowed.is_relative_to(needed) for needed in required)
            ):
                raise ValueError(f"sandbox read grant shadows credential deny: {allowed}")
