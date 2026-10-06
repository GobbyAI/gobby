"""Managed policy preserves provider operation without allowing host control edits."""

from pathlib import Path

import pytest

from gobby.agents.sandbox import ResolvedSandboxPaths, SandboxConfig, compute_sandbox_paths

pytestmark = pytest.mark.unit

PROVIDERS = ("claude", "codex", "qwen", "droid", "grok", "agy", "gemini")
CONTROL_FILES = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude.json",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".qwen/settings.json",
    ".qwen/trustedFolders.json",
    ".factory/hooks.json",
    ".factory/settings.json",
    ".factory/settings.local.json",
    ".grok/hooks/added.json",
    ".grok/config.toml",
    ".grok/hooks-paths",
    ".grok/managed_config.toml",
    ".grok/requirements.toml",
    ".grok/trusted_folders.toml",
    ".grok/trusted-hook-projects",
    ".grok/trusted-plugins",
    ".grok/sandbox.toml",
    ".grok/bin/grok",
    ".cursor/hooks.json",
    ".gemini/config/hooks.json",
    ".gemini/settings.json",
    ".gemini/trustedFolders.json",
    ".config/gemini/settings.json",
    ".claude/plugins/cache/public/hooks/hooks.json",
    ".claude/plugins/marketplaces/public/.mcp.json",
    ".claude/plugins/synced/public/.mcp.json",
    ".claude/plugins/installed_plugins.json",
    ".claude/plugins/known_marketplaces.json",
    ".codex/plugins/cache/public/.mcp.json",
    ".qwen/extensions/public/qwen-extension.json",
    ".qwen/extension-store/state.json",
    ".gemini/extensions/public/gemini-extension.json",
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
    ".factory/plugins/cache/public/hooks/hooks.json",
    ".factory/plugins/installed_plugins.json",
    ".factory/plugins/known_marketplaces.json",
)
CREDENTIAL_FILES = (
    ".ssh/id_ed25519",
    ".aws/credentials",
    ".gnupg/private-keys-v1.d/key",
    ".kube/config",
    ".config/gcloud/application_default_credentials.json",
    ".config/gh/hosts.yml",
    ".netrc",
    ".docker/config.json",
)


def _contains(roots: list[str], path: Path) -> bool:
    return any(path == Path(root) or path.is_relative_to(Path(root)) for root in roots)


def _can_read(paths: ResolvedSandboxPaths, path: Path) -> bool:
    return _contains(paths.read_paths, path) or not _contains(paths.deny_read_paths, path)


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_provider_control_files_cannot_be_written(
    provider: str, isolated_home: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt"),
        str(workspace),
        provider=provider,
        env={"PATH": ""},
    )
    for relative in CONTROL_FILES:
        control = isolated_home / relative
        assert _contains(paths.deny_write_paths, control), (provider, relative)
    # Workspace grants must not enable persistent project-local hooks either.
    for relative in CONTROL_FILES:
        if relative.startswith(
            (".claude/", ".codex/", ".qwen/", ".factory/", ".gemini/", ".grok/", ".cursor/")
        ):
            assert _contains(paths.deny_write_paths, workspace / relative), relative


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_user_credentials_denied_with_only_safe_ssh_child(
    provider: str, isolated_home: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt"),
        str(workspace),
        provider=provider,
        env={"PATH": ""},
    )
    for relative in CREDENTIAL_FILES:
        credential = isolated_home / relative
        assert _contains(paths.deny_read_paths, credential), relative
        assert not _can_read(paths, credential), (provider, relative)
    known_hosts = isolated_home / ".ssh/known_hosts"
    assert str(known_hosts) in paths.read_paths
    assert _can_read(paths, known_hosts)
    assert not _can_read(paths, isolated_home / ".ssh/config")


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_foreign_provider_credentials_denied(
    provider: str, isolated_home: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt"),
        str(workspace),
        provider=provider,
        env={"PATH": ""},
    )
    credentials = {
        "claude": ".claude/.credentials.json",
        "codex": ".codex/auth.json",
        "qwen": ".qwen/oauth_creds.json",
        "droid": ".factory/auth.json",
        "grok": ".grok/auth.json",
        "gemini": ".gemini/oauth_creds.json",
        "agy": ".gemini/antigravity-cli/auth.json",
    }
    for owner, relative in credentials.items():
        if owner == provider or (provider == "gemini" and owner == "agy"):
            continue
        credential = isolated_home / relative
        assert _contains(paths.deny_read_paths, credential), (provider, owner)
        assert not _can_read(paths, credential), (provider, owner)


@pytest.mark.parametrize("grant", (".ssh/id_ed25519", ".config/gh/hosts.yml", ".codex/auth.json"))
async def test_extra_reads_cannot_reenable_credentials(
    grant: str, isolated_home: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="credential deny"):
        await compute_sandbox_paths(
            SandboxConfig(
                enabled=True, backend="srt", extra_read_paths=[str(isolated_home / grant)]
            ),
            str(workspace),
            provider="claude",
            env={"PATH": ""},
        )


async def test_selected_provider_alias_cannot_grant_user_credential_store(
    isolated_home: Path, tmp_path: Path
) -> None:
    ssh = isolated_home / ".ssh"
    ssh.mkdir()
    (isolated_home / ".codex").symlink_to(ssh, target_is_directory=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="credential deny"):
        await compute_sandbox_paths(
            SandboxConfig(enabled=True, backend="srt"),
            str(workspace),
            provider="codex",
            env={"PATH": ""},
        )


@pytest.mark.parametrize("grant", [".", ".claude/settings.json"])
async def test_extra_workspace_grant_cannot_install_persistent_hooks(
    isolated_home: Path, tmp_path: Path, grant: str
) -> None:
    workspace = tmp_path / "workspace"
    extra_checkout = tmp_path / "other-checkout"
    workspace.mkdir()
    extra_checkout.mkdir()
    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt", extra_write_paths=[str(extra_checkout / grant)]),
        str(workspace),
        provider="claude",
        env={"PATH": ""},
    )
    assert _contains(paths.write_paths, extra_checkout / grant)
    assert _contains(paths.deny_write_paths, extra_checkout / ".claude/settings.json")
    assert _contains(paths.deny_write_paths, extra_checkout / ".codex/hooks.json")


async def test_claude_plugin_runtime_caches_remain_writable(
    isolated_home: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt"),
        str(workspace),
        provider="claude",
        env={"PATH": ""},
    )
    for relative in (
        ".claude/plugins/plugin-directory-cache-v2.json",
        ".claude/plugins/install-counts-cache.json",
        ".claude/plugins/data/public/state",
    ):
        target = isolated_home / relative
        assert _contains(paths.write_paths, target)
        assert not _contains(paths.deny_write_paths, target)
