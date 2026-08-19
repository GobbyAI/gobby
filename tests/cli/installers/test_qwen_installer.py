"""Tests for the Qwen CLI installer module."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.cli.installers.qwen import install_qwen, uninstall_qwen
from gobby.cli.utils import get_install_dir as get_real_install_dir

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

        source_template = get_real_install_dir() / "qwen" / "hooks-template.json"
        (qwen_dir / "hooks-template.json").write_text(source_template.read_text())

        shared_scripts = install_dir / "shared" / "scripts"
        shared_scripts.mkdir(parents=True)
        (shared_scripts / "agent_shutdown.sh").write_text("#!/usr/bin/env bash\necho shutdown\n")

        with patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/usr/local/bin/ghook",
        ):
            yield install_dir

    @pytest.mark.parametrize("hook_timeout_seconds", [0, -1])
    def test_install_qwen_rejects_non_positive_timeout_before_filesystem_activity(
        self,
        project_path: Path,
        hook_timeout_seconds: int,
    ) -> None:
        result = install_qwen(
            project_path,
            mode="project",
            hook_timeout_seconds=hook_timeout_seconds,
        )

        assert result["success"] is False
        assert result["error"] == "hook_timeout_seconds must be positive"
        assert not (project_path / ".qwen").exists()

    def test_install_qwen_success(
        self, project_path: Path, mock_install_dir: Path, temp_dir: Path
    ) -> None:
        template_hooks = json.loads(
            (mock_install_dir / "qwen" / "hooks-template.json").read_text()
        )["hooks"]
        user_hooks = {hook_type: f"user-{hook_type}" for hook_type in template_hooks}
        settings_file = project_path / ".qwen" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        existing_hooks = {
            hook_type: [
                {
                    "custom": "preserve-group-metadata",
                    "hooks": [
                        {"type": "command", "command": user_command},
                        {
                            "type": "command",
                            "command": "/old/ghook --gobby-owned --cli=qwen --type=stale",
                        },
                    ],
                }
            ]
            for hook_type, user_command in user_hooks.items()
        }
        settings_file.write_text(
            json.dumps(
                {
                    "hooks": existing_hooks,
                    "general": {"enableHooks": True, "theme": "dark"},
                    "disableAllHooks": True,
                }
            )
        )
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
                "gobby.cli.installers.qwen.install_router_skills_as_cli_skills",
                return_value=["gobby/", "g/"],
            ),
            patch(
                "gobby.cli.installers.qwen.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_qwen(
                project_path,
                mode="project",
                hook_timeout_seconds=150,
            )

        assert result["success"] is True
        assert result["error"] is None
        assert result["hooks_installed"] == list(template_hooks)
        assert result["commands_installed"] == ["command1.md", "gobby/", "g/"]
        assert result["plugins_installed"] == ["plugin1.py"]
        assert result["mcp_configured"] is True
        assert result["scripts_installed"] == ["agent_shutdown.sh"]
        assert result["trust"]["success"] is True
        assert os.environ["GOBBY_HOME"] in result["trust"]["paths"]

        with open(settings_file) as f:
            settings = json.load(f)

        assert settings["disableAllHooks"] is False
        assert settings["general"] == {"theme": "dark"}
        assert settings["ui"]["hideTips"] is True
        assert settings["context"]["fileName"] == ["AGENTS.md", "QWEN.md"]
        assert "hooks" in settings
        for hook_type, user_command in user_hooks.items():
            groups = settings["hooks"][hook_type]
            assert groups[0]["custom"] == "preserve-group-metadata"
            commands = [handler["command"] for group in groups for handler in group["hooks"]]
            assert user_command in commands
            assert not any("--type=stale" in command for command in commands)
            assert sum("--gobby-owned" in command for command in commands) == 1
            gobby_handler = next(
                handler
                for group in groups
                for handler in group["hooks"]
                if "--gobby-owned" in handler["command"]
            )
            assert gobby_handler["timeout"] == 150_000
        assert (temp_dir / ".qwen" / "projects.json").exists()
        assert (temp_dir / ".qwen" / "trustedFolders.json").exists()

    def test_install_qwen_preserves_user_context_file_name(
        self, project_path: Path, mock_install_dir: Path, temp_dir: Path
    ) -> None:
        settings_file = project_path / ".qwen" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({"context": {"fileName": "MY_CONTEXT.md"}}))
        with (
            patch("gobby.cli.installers.qwen.get_install_dir", return_value=mock_install_dir),
            patch("gobby.cli.installers.qwen.install_shared_content", return_value={}),
            patch("gobby.cli.installers.qwen.install_cli_content", return_value={}),
            patch(
                "gobby.cli.installers.qwen.install_router_skills_as_cli_skills",
                return_value=[],
            ),
            patch(
                "gobby.cli.installers.qwen.configure_mcp_server_json",
                return_value={"success": True, "added": True},
            ),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            result = install_qwen(project_path, mode="project")

        assert result["success"] is True
        with open(settings_file) as f:
            settings = json.load(f)
        assert settings["context"]["fileName"] == "MY_CONTEXT.md"

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
                "gobby.cli.installers.qwen.install_router_skills_as_cli_skills",
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
                        "SessionStart": [
                            {
                                "hooks": [
                                    {"command": "user-session-start"},
                                    {
                                        "command": "ghook --gobby-owned --cli=qwen --type=SessionStart"
                                    },
                                ]
                            }
                        ],
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
        assert result["hooks_removed"] == ["SessionStart"]
        assert result["mcp_removed"] is True
        assert result["files_removed"] == []

        backup_file = qwen_path / "settings.json.1234567890.backup"
        assert backup_file.exists()
        assert hooks_dir.exists()
        assert (hooks_dir / "session-start.sh").exists()
        assert (hooks_dir / "session-end.sh").exists()

        with open(settings_file) as f:
            settings = json.load(f)
        assert settings["hooks"]["SessionStart"][0]["hooks"] == [{"command": "user-session-start"}]

    def test_uninstall_qwen_global_mode_uses_home_directory(
        self, project_path: Path, temp_dir: Path
    ) -> None:
        qwen_path = temp_dir / ".qwen"
        qwen_path.mkdir(parents=True)
        settings_file = qwen_path / "settings.json"
        settings_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "command": "ghook --gobby-owned --cli=qwen --type=SessionStart"
                                    }
                                ]
                            }
                        ]
                    }
                }
            )
        )

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
