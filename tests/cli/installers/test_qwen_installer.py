"""Tests for the Qwen CLI installer module."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.cli.installers.qwen import install_qwen, uninstall_qwen

pytestmark = pytest.mark.unit


class TestInstallQwen:
    """Tests for install_qwen function."""

    @pytest.fixture
    def project_path(self, temp_dir: Path) -> Path:
        project = temp_dir / "test-project"
        project.mkdir(parents=True)
        return project

    @pytest.fixture
    def mock_install_dir(self, temp_dir: Path) -> Path:
        install_dir = temp_dir / "install"
        qwen_dir = install_dir / "qwen"
        qwen_dir.mkdir(parents=True)

        template = qwen_dir / "hooks-template.json"
        template.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": {"command": "uv run $HOOKS_DIR/hook_dispatcher.py"},
                        "SessionEnd": {"command": "uv run $HOOKS_DIR/hook_dispatcher.py"},
                    }
                }
            )
        )

        shared_scripts = install_dir / "shared" / "scripts"
        shared_scripts.mkdir(parents=True)
        (shared_scripts / "agent_shutdown.sh").write_text("#!/usr/bin/env bash\necho shutdown\n")

        with patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/usr/local/bin/ghook",
        ):
            yield install_dir

    def test_install_qwen_success(
        self, project_path: Path, mock_install_dir: Path, temp_dir: Path
    ) -> None:
        with (
            patch("gobby.cli.installers.qwen.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.qwen.install_shared_content",
                return_value={"plugins": ["plugin1.py"]},
            ),
            patch(
                "gobby.cli.installers.qwen.install_cli_content",
                return_value={"commands": ["command1.md"]},
            ),
            patch(
                "gobby.cli.installers.qwen.install_router_skills_as_gemini_skills",
                return_value=["gobby/", "g/"],
            ),
            patch(
                "gobby.cli.installers.qwen.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_qwen(project_path, mode="project")

        assert result["success"] is True
        assert result["error"] is None
        assert result["hooks_installed"] == ["SessionStart", "SessionEnd"]
        assert result["commands_installed"] == ["command1.md", "gobby/", "g/"]
        assert result["plugins_installed"] == ["plugin1.py"]
        assert result["mcp_configured"] is True
        assert result["scripts_installed"] == ["agent_shutdown.sh"]
        assert result["trust"]["success"] is True
        assert os.environ["GOBBY_HOME"] in result["trust"]["paths"]

        settings_file = project_path / ".qwen" / "settings.json"
        with open(settings_file) as f:
            settings = json.load(f)

        assert settings["general"]["enableHooks"] is True
        assert settings["ui"]["hideTips"] is True
        assert "hooks" in settings
        assert (temp_dir / ".qwen" / "projects.json").exists()
        assert (temp_dir / ".qwen" / "trustedFolders.json").exists()

    def test_install_qwen_returns_error_for_malformed_settings_json(
        self, project_path: Path, mock_install_dir: Path, temp_dir: Path
    ) -> None:
        settings_file = project_path / ".qwen" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text("{not-json")

        with (
            patch("gobby.cli.installers.qwen.get_install_dir", return_value=mock_install_dir),
            patch(
                "gobby.cli.installers.qwen.install_shared_content",
                return_value={"plugins": ["plugin1.py"]},
            ),
            patch(
                "gobby.cli.installers.qwen.install_cli_content",
                return_value={"commands": ["command1.md"]},
            ),
            patch(
                "gobby.cli.installers.qwen.install_router_skills_as_gemini_skills",
                return_value=["gobby/", "g/"],
            ),
            patch(
                "gobby.cli.installers.qwen.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_qwen(project_path, mode="project")

        assert result["success"] is False
        assert "settings.json is malformed" in result["error"]
        assert settings_file.read_text() == "{not-json"

    def test_uninstall_qwen_removes_hooks(self, project_path: Path, temp_dir: Path) -> None:
        qwen_path = project_path / ".qwen"
        qwen_path.mkdir(parents=True)
        settings_file = qwen_path / "settings.json"
        hooks_dir = qwen_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "session-start.sh").write_text("#!/usr/bin/env bash\n")
        (hooks_dir / "session-end.sh").write_text("#!/usr/bin/env bash\n")
        settings_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": {"command": "start"},
                        "SessionEnd": {"command": "end"},
                    },
                    "general": {"enableHooks": True},
                }
            )
        )

        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch(
                "gobby.cli.installers.qwen.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
            patch("gobby.cli.installers.qwen.time") as mock_time,
        ):
            mock_time.time.return_value = 1234567890
            result = uninstall_qwen(project_path)

        assert result["success"] is True
        assert result["hooks_removed"] == ["SessionStart", "SessionEnd"]
        assert result["mcp_removed"] is True
        assert sorted(result["files_removed"]) == [
            "hooks/session-end.sh",
            "hooks/session-start.sh",
        ]

        backup_file = qwen_path / "settings.json.1234567890.backup"
        assert backup_file.exists()
        assert not hooks_dir.exists()

        with open(settings_file) as f:
            settings = json.load(f)
        assert "hooks" not in settings

    def test_uninstall_qwen_global_mode_uses_home_directory(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        qwen_path = temp_dir / ".qwen"
        qwen_path.mkdir(parents=True)
        settings_file = qwen_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {"SessionStart": {"command": "start"}}}))

        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch(
                "gobby.cli.installers.qwen.remove_mcp_server_json",
                return_value={"success": True, "removed": True},
            ),
        ):
            result = uninstall_qwen(project_path, mode="global")

        assert result["success"] is True
        with open(settings_file) as f:
            settings = json.load(f)
        assert "hooks" not in settings
