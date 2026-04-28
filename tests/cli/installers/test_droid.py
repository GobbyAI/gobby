"""Tests for the Factory Droid CLI installer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
from gobby.cli.installers.droid import install_droid, uninstall_droid
from gobby.install.shared.hooks import validate_settings

pytestmark = pytest.mark.unit


@pytest.fixture
def project_path(temp_dir: Path) -> Path:
    project = temp_dir / "project"
    project.mkdir()
    return project


@pytest.fixture
def droid_env(temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GOBBY_HOOKS_DIR", raising=False)
    monkeypatch.delenv("GOBBY_DROID_HOOKS_FILE", raising=False)
    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/Users/test/.gobby/bin/ghook",
        ),
        patch("gobby.cli.installers.droid.install_shared_content", return_value={"plugins": []}),
        patch("gobby.cli.installers.droid.install_cli_content", return_value={"commands": []}),
    ):
        yield temp_dir


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_install_droid_global_writes_hooks_and_mcp(
    project_path: Path,
    droid_env: Path,
) -> None:
    result = install_droid(project_path, mode="global")

    assert result["success"] is True
    assert tuple(result["hooks_installed"]) == DROID_PASCAL_HOOK_NAMES

    hooks_file = droid_env / ".factory" / "hooks" / "hooks.json"
    hooks = _load_json(hooks_file)["hooks"]
    assert tuple(hooks) == DROID_PASCAL_HOOK_NAMES
    for hook_type in DROID_PASCAL_HOOK_NAMES:
        command = hooks[hook_type][0]["hooks"][0]["command"]
        assert (
            command == f"/Users/test/.gobby/bin/ghook --gobby-owned --cli=droid --type={hook_type}"
        )

    mcp = _load_json(droid_env / ".factory" / "mcp.json")
    assert mcp["mcpServers"]["gobby"]["type"] == "stdio"
    assert mcp["mcpServers"]["gobby"]["args"] == ["mcp-server"]


def test_install_droid_project_mode_writes_project_hooks(
    project_path: Path,
    droid_env: Path,
) -> None:
    result = install_droid(project_path, mode="project")

    assert result["success"] is True
    assert (project_path / ".factory" / "hooks" / "hooks.json").exists()
    assert (project_path / ".factory" / "mcp.json").exists()


def test_install_droid_preserves_existing_mcp_servers(
    project_path: Path,
    droid_env: Path,
) -> None:
    mcp_file = droid_env / ".factory" / "mcp.json"
    mcp_file.parent.mkdir(parents=True)
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {"command": "node"},
                    "beta": {"command": "python"},
                }
            }
        )
    )

    result = install_droid(project_path, mode="global")

    assert result["success"] is True
    mcp_servers = _load_json(mcp_file)["mcpServers"]
    assert set(mcp_servers) == {"alpha", "beta", "gobby"}
    assert mcp_servers["gobby"]["type"] == "stdio"


def test_install_droid_honors_hook_path_overrides(
    project_path: Path,
    droid_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks_dir = droid_env / "custom-hooks"
    monkeypatch.setenv("GOBBY_HOOKS_DIR", str(hooks_dir))
    result = install_droid(project_path, mode="global")
    assert result["success"] is True
    assert (hooks_dir / "hooks.json").exists()

    specific_hooks = droid_env / "specific" / "hooks.json"
    monkeypatch.setenv("GOBBY_DROID_HOOKS_FILE", str(specific_hooks))
    result = install_droid(project_path, mode="global")
    assert result["success"] is True
    assert specific_hooks.exists()


def test_install_droid_is_idempotent_and_preserves_file_text(
    project_path: Path,
    droid_env: Path,
) -> None:
    first = install_droid(project_path, mode="global")
    hooks_file = droid_env / ".factory" / "hooks" / "hooks.json"
    original_text = hooks_file.read_text()

    second = install_droid(project_path, mode="global")

    assert first["success"] is True
    assert second["success"] is True
    assert second["already_configured"] is True
    assert hooks_file.read_text() == original_text


def test_install_droid_creates_backup_when_rewriting_existing_hooks(
    project_path: Path,
    droid_env: Path,
) -> None:
    hooks_file = droid_env / ".factory" / "hooks" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(json.dumps({"hooks": {"Other": [{"command": "custom"}]}}))

    with patch("gobby.cli.installers.droid.time.time", return_value=1234567890):
        result = install_droid(project_path, mode="global")

    assert result["success"] is True
    assert (hooks_file.parent / "hooks.json.1234567890.backup").exists()
    assert "Other" in _load_json(hooks_file)["hooks"]


def test_uninstall_droid_removes_only_gobby_entries(
    project_path: Path,
    droid_env: Path,
) -> None:
    hooks_file = droid_env / ".factory" / "hooks" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "custom"}]},
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "ghook --gobby-owned --cli=droid --type=PreToolUse",
                                }
                            ]
                        },
                    ],
                    "Custom": [{"hooks": [{"type": "command", "command": "custom"}]}],
                }
            }
        )
    )
    mcp_file = droid_env / ".factory" / "mcp.json"
    mcp_file.write_text(
        json.dumps({"mcpServers": {"gobby": {"command": "gobby"}, "other": {"command": "node"}}})
    )

    result = uninstall_droid(project_path, mode="global")

    assert result["success"] is True
    assert result["hooks_removed"] == ["PreToolUse"]
    hooks = _load_json(hooks_file)["hooks"]
    assert hooks["PreToolUse"] == [{"hooks": [{"type": "command", "command": "custom"}]}]
    assert "Custom" in hooks
    assert set(_load_json(mcp_file)["mcpServers"]) == {"other"}


def test_install_droid_warns_on_empty_project_hooks_override(
    project_path: Path,
    droid_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings_file = project_path / ".factory" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"hooks": {}}))

    result = install_droid(project_path, mode="global")

    assert result["success"] is True
    assert "Project-level hooks config" in capsys.readouterr().err


def test_install_droid_warns_when_ghook_is_outdated(
    project_path: Path,
    droid_env: Path,
) -> None:
    with patch("gobby.cli.installers.droid.get_ghook_version", return_value="0.99.0", create=True):
        result = install_droid(project_path, mode="global")

    assert result["success"] is True
    assert (droid_env / ".factory" / "hooks" / "hooks.json").exists()
    assert any("ghook" in warning and "upgrade" in warning for warning in result["warnings"])


def test_install_droid_does_not_warn_when_ghook_meets_minimum(
    project_path: Path,
    droid_env: Path,
) -> None:
    with patch("gobby.cli.installers.droid.get_ghook_version", return_value="1.0.0", create=True):
        result = install_droid(project_path, mode="global")

    assert result["success"] is True
    assert "warnings" not in result


@pytest.mark.parametrize("version", [None, "not-a-version"])
def test_install_droid_warns_when_ghook_version_cannot_be_checked(
    project_path: Path,
    droid_env: Path,
    version: str | None,
) -> None:
    with patch("gobby.cli.installers.droid.get_ghook_version", return_value=version, create=True):
        result = install_droid(project_path, mode="global")

    assert result["success"] is True
    assert (droid_env / ".factory" / "hooks" / "hooks.json").exists()
    assert any("ghook" in warning and "version" in warning for warning in result["warnings"])


def test_validate_settings_accepts_fresh_droid_install(
    project_path: Path,
    droid_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = install_droid(project_path, mode="global")
    assert result["success"] is True

    monkeypatch.setattr(validate_settings, "find_project_root", lambda: droid_env)
    assert validate_settings.validate(validate_settings.CLI_VALIDATION_CONFIGS["droid"]) == 0
