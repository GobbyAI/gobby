"""Install paths must follow the configured Gobby home."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli import install_setup
from gobby.cli.installers import agy, claude, codex, droid, git_hooks, grok, qdrant, shared
from gobby.cli.uninstall import _remove_global_hook_files

pytestmark = pytest.mark.unit


@pytest.fixture
def relocated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setattr(Path, "home", lambda: user_home)
    home = tmp_path / "gobby"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.delenv("GOBBY_HOOKS_DIR", raising=False)
    return home


def test_bootstrap_creation_and_existing_lookup_use_gobby_home(
    relocated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files_home = tmp_path / "files"
    files_home.mkdir()
    monkeypatch.setattr(install_setup, "get_install_dir", lambda: tmp_path / "missing")
    result = install_setup.ensure_daemon_config(files_home=files_home)
    bootstrap = relocated_home / "bootstrap.yaml"
    assert result["created"] is True
    assert result["path"] == str(bootstrap)
    assert bootstrap.is_file()
    before = bootstrap.stat()
    existing = install_setup.ensure_daemon_config()
    assert existing == {"created": False, "path": str(bootstrap)}
    assert bootstrap.stat() == before
    assert not (Path.home() / ".gobby").exists()


@pytest.mark.parametrize("override", [False, True])
def test_claude_global_install_uses_relocated_hooks(
    relocated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: bool
) -> None:
    hooks_dir = tmp_path / "override-hooks" if override else relocated_home / "hooks"
    if override:
        monkeypatch.setenv("GOBBY_HOOKS_DIR", str(hooks_dir))
    source = tmp_path / "install"
    template = source / "claude" / "hooks-template.json"
    template.parent.mkdir(parents=True)
    template.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": []}]}}))
    helper = source / "shared" / "hooks" / "validate_settings.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("# temporary hook helper\n")
    project = tmp_path / "project"
    project.mkdir()
    with (
        patch.object(claude, "get_install_dir", return_value=source),
        patch.object(shared, "get_install_dir", return_value=source),
        patch.object(claude, "install_shared_content", return_value={}),
        patch.object(claude, "install_cli_content", return_value={}),
        patch.object(claude, "install_router_skills_as_commands", return_value=[]),
        patch.object(claude, "configure_mcp_server_json", return_value={"success": True}),
        patch.object(claude, "seed_gobby_home_trust", return_value={"success": True}),
        patch.object(claude, "rewrite_hook_template_commands") as rewrite,
        patch.object(claude, "_configure_statusline") as statusline,
    ):
        result = claude.install_claude(project, mode="global")
    assert result["success"] is True
    assert (hooks_dir / "validate_settings.py").read_text() == helper.read_text()
    assert (Path.home() / ".claude" / "settings.json").is_file()
    assert rewrite.call_args.kwargs["hooks_dir"] == hooks_dir
    assert statusline.call_args.args[1] == hooks_dir
    assert not (Path.home() / ".gobby").exists()


@pytest.mark.parametrize("provider", [agy, codex, droid])
@pytest.mark.parametrize("override", [False, True])
def test_provider_hooks_resolve_configured_home(
    relocated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ModuleType,
    override: bool,
) -> None:
    expected = tmp_path / "override-hooks" if override else relocated_home / "hooks"
    if override:
        monkeypatch.setenv("GOBBY_HOOKS_DIR", str(expected))
    resolver = getattr(provider, "_get_hooks_dir" if provider is codex else "_global_hooks_dir")
    assert resolver() == expected


@pytest.mark.parametrize("binary", ["gcode", "ghook", "gclient", "gterm"])
def test_binary_installer_builds_in_configured_home(relocated_home: Path, binary: str) -> None:
    implementation = importlib.import_module(f"gobby.cli.install_setup_{binary}")
    module = MagicMock()
    module.Path = Path
    module.sys.platform = "darwin"
    module.platform.machine.return_value = "arm64"
    setattr(module, f"_{binary.upper()}_BIN_NAME", binary)
    setattr(module, f"_{binary.upper()}_TARGETS", {("darwin", "arm64"): "target"})
    getattr(module, f"_get_installed_{binary}_version").return_value = None
    build_name = (
        "_install_ghook_from_workspace"
        if binary == "ghook"
        else f"_install_{binary}_from_submodule"
    )
    build = getattr(module, build_name)

    def build_stub(bin_dir: Path) -> bool:
        (bin_dir / binary).write_bytes(b"temporary test binary")
        return True

    build.side_effect = build_stub
    module._ensure_gobby_bin_on_path.return_value = {}
    with (
        patch.object(implementation, "is_published", return_value=False, create=True),
        patch.object(implementation, f"probe_{binary}_version", return_value="0.0.0", create=True),
    ):
        result = getattr(implementation, f"install_{binary}")(module)
    assert result["installed"] is True
    build.assert_called_once_with(relocated_home / "bin")
    assert (relocated_home / "bin" / binary).is_file()
    assert not (Path.home() / ".gobby").exists()


def test_hook_uninstall_removes_only_configured_home(relocated_home: Path) -> None:
    installed = relocated_home / "hooks" / "validate_settings.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("temporary hook")
    default = Path.home() / ".gobby" / "hooks" / "validate_settings.py"
    default.parent.mkdir(parents=True)
    default.write_text("default hook")
    _remove_global_hook_files()
    assert not installed.exists()
    assert default.read_text() == "default hook"


def test_qdrant_install_uses_configured_home(relocated_home: Path) -> None:
    with (
        patch.object(qdrant, "managed_services_lock", return_value=nullcontext()) as lock,
        patch.object(qdrant, "_install_qdrant_locked", return_value={"success": True}) as install,
    ):
        result = qdrant.install_qdrant()
    assert result["success"] is True
    lock.assert_called_once_with(relocated_home, operation="qdrant installer refresh")
    install.assert_called_once_with(gobby_home=relocated_home, port=qdrant.DEFAULT_QDRANT_PORT)


def test_grok_install_uses_configured_hooks(relocated_home: Path, tmp_path: Path) -> None:
    source = tmp_path / "install"
    template = source / "grok" / "hooks-template.json"
    template.parent.mkdir(parents=True)
    template.write_text(json.dumps({"hooks": {}}))
    with (
        patch.object(grok, "_grok_ghook_support_error", return_value=None),
        patch.object(grok, "get_install_dir", return_value=source),
        patch.object(grok, "install_global_hooks"),
        patch.object(grok, "install_shared_content", return_value={}),
        patch.object(grok, "install_router_skills_as_cli_skills", return_value=[]),
        patch.object(grok, "rewrite_hook_template_commands") as rewrite,
    ):
        result = grok.install_grok(tmp_path)
    assert result["success"] is True
    assert rewrite.call_args.kwargs["hooks_dir"] == relocated_home / "hooks"


def test_git_reindex_hook_uses_configured_binary(relocated_home: Path, tmp_path: Path) -> None:
    binary = relocated_home / "bin" / "gcode"
    binary.parent.mkdir(parents=True)
    invoked = tmp_path / "invoked"
    binary.write_text('#!/bin/sh\nprintf "%s" "$*" > "$INVOCATION_FILE"\n')
    binary.chmod(0o755)
    script = git_hooks._CODE_INDEX_REINDEX_BODY + "\nwait\n"
    completed = subprocess.run(
        ["sh", "-c", script],
        env={**os.environ, "CHANGED_FILES": "changed.py", "INVOCATION_FILE": str(invoked)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    assert invoked.read_text() == "index --quiet --skip-if-locked --files=changed.py"
