"""Tests for the Copilot CLI installer."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.cli.installers.copilot import install_copilot, uninstall_copilot

pytestmark = pytest.mark.unit


def test_install_copilot_global_mode(tmp_path: Path) -> None:
    result = install_copilot(tmp_path, mode="global")
    assert result["success"] is True
    assert result["skipped"] is True
    assert "global hooks" in result["skip_reason"]


def test_install_copilot_missing_template(tmp_path: Path) -> None:
    with patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path):
        result = install_copilot(tmp_path, mode="project")
        assert result["success"] is False
        assert "Missing source files" in result["error"]


def test_install_copilot_global_hooks_error(tmp_path: Path) -> None:
    copilot_install_dir = tmp_path / "copilot"
    copilot_install_dir.mkdir(parents=True)
    (copilot_install_dir / "hooks-template.json").write_text("{}")

    with patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path):
        with patch(
            "gobby.cli.installers.copilot.install_global_hooks", side_effect=OSError("denied")
        ):
            result = install_copilot(tmp_path, mode="project")
            assert result["success"] is False
            assert "Failed to install hook files" in result["error"]


def test_install_copilot_shared_content_error(tmp_path: Path) -> None:
    copilot_install_dir = tmp_path / "copilot"
    copilot_install_dir.mkdir(parents=True)
    (copilot_install_dir / "hooks-template.json").write_text('{"hooks": {"pre-commit": "test"}}')

    with (
        patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path),
        patch("gobby.cli.installers.copilot.install_global_hooks"),
        patch(
            "gobby.cli.installers.copilot.install_shared_content",
            side_effect=Exception("Failed sync"),
        ),
    ):
        # Should not fail entirely
        result = install_copilot(tmp_path, mode="project")
        assert result["success"] is True
        assert "pre-commit" in result["hooks_installed"]


def test_install_copilot_backup_error(tmp_path: Path) -> None:
    copilot_install_dir = tmp_path / "copilot"
    copilot_install_dir.mkdir(parents=True)
    (copilot_install_dir / "hooks-template.json").write_text("{}")

    hooks_file = tmp_path / ".copilot" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text("{}")

    with (
        patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path),
        patch("gobby.cli.installers.copilot.install_global_hooks"),
        patch("gobby.cli.installers.copilot.install_shared_content", return_value={}),
        patch("gobby.cli.installers.copilot.copy2", side_effect=OSError("copy fail")),
    ):
        result = install_copilot(tmp_path, mode="project")
        assert result["success"] is False
        assert "Failed to create backup" in result["error"]


def test_install_copilot_existing_file_json_decode_error(tmp_path: Path) -> None:
    copilot_install_dir = tmp_path / "copilot"
    copilot_install_dir.mkdir(parents=True)
    (copilot_install_dir / "hooks-template.json").write_text('{"hooks": {"pre-commit": "test"}}')

    hooks_file = tmp_path / ".copilot" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text("{bad-json")

    with (
        patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path),
        patch("gobby.cli.installers.copilot.install_global_hooks"),
        patch("gobby.cli.installers.copilot.install_shared_content", return_value={}),
        patch("gobby.cli.installers.copilot.copy2"),
    ):
        result = install_copilot(tmp_path, mode="project")
        assert result["success"] is True


def test_install_copilot_existing_file_os_error(tmp_path: Path) -> None:
    copilot_install_dir = tmp_path / "copilot"
    copilot_install_dir.mkdir(parents=True)
    (copilot_install_dir / "hooks-template.json").write_text('{"hooks": {"pre-commit": "test"}}')

    hooks_file = tmp_path / ".copilot" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text("{}")

    with (
        patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path),
        patch("gobby.cli.installers.copilot.install_global_hooks"),
        patch("gobby.cli.installers.copilot.install_shared_content", return_value={}),
        patch("gobby.cli.installers.copilot.copy2"),
        patch("builtins.open", side_effect=OSError("read error")),
    ):
        result = install_copilot(tmp_path, mode="project")
        assert result["success"] is False
        assert "Failed to read hooks.json" in result["error"]


def test_install_copilot_template_read_errors(tmp_path: Path) -> None:
    copilot_install_dir = tmp_path / "copilot"
    copilot_install_dir.mkdir(parents=True)

    # Needs to exist for earlier check, but fail on read
    template_file = copilot_install_dir / "hooks-template.json"
    template_file.write_text("{}")

    with (
        patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path),
        patch("gobby.cli.installers.copilot.install_global_hooks"),
        patch("gobby.cli.installers.copilot.install_shared_content", return_value={}),
    ):
        # Test OSError
        # We only throw OSError for the template file read
        original_open = open

        def mock_open(*args: Any, **kwargs: Any) -> Any:
            if str(args[0]) == str(template_file):
                raise OSError("open error")
            return original_open(*args, **kwargs)

        with patch("builtins.open", side_effect=mock_open):
            result = install_copilot(tmp_path, mode="project")
            assert result["success"] is False
            assert "Failed to read hooks template" in result["error"]


def test_install_copilot_template_json_error(tmp_path: Path) -> None:
    copilot_install_dir = tmp_path / "copilot"
    copilot_install_dir.mkdir(parents=True)
    (copilot_install_dir / "hooks-template.json").write_text("{bad-template")

    with (
        patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path),
        patch("gobby.cli.installers.copilot.install_global_hooks"),
        patch("gobby.cli.installers.copilot.install_shared_content", return_value={}),
    ):
        result = install_copilot(tmp_path, mode="project")
        assert result["success"] is False
        assert "Failed to parse hooks template" in result["error"]


def test_install_copilot_write_error(tmp_path: Path) -> None:
    copilot_install_dir = tmp_path / "copilot"
    copilot_install_dir.mkdir(parents=True)
    (copilot_install_dir / "hooks-template.json").write_text('{"hooks": {"pre-commit": "test"}}')

    with (
        patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path),
        patch("gobby.cli.installers.copilot.install_global_hooks"),
        patch("gobby.cli.installers.copilot.install_shared_content", return_value={}),
        patch("gobby.cli.installers.copilot.tempfile.mkstemp", side_effect=OSError("tmp fail")),
    ):
        result = install_copilot(tmp_path, mode="project")
        assert result["success"] is False
        assert "Failed to write hooks.json" in result["error"]


def test_install_copilot_write_error_with_backup_restore(tmp_path: Path) -> None:
    copilot_install_dir = tmp_path / "copilot"
    copilot_install_dir.mkdir(parents=True)
    (copilot_install_dir / "hooks-template.json").write_text('{"hooks": {"pre-commit": "test"}}')

    hooks_file = tmp_path / ".copilot" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text("{}")

    with (
        patch("gobby.cli.installers.copilot.get_install_dir", return_value=tmp_path),
        patch("gobby.cli.installers.copilot.install_global_hooks"),
        patch("gobby.cli.installers.copilot.install_shared_content", return_value={}),
        patch("gobby.cli.installers.copilot.os.replace", side_effect=OSError("replace error")),
    ):
        result = install_copilot(tmp_path, mode="project")
        assert result["success"] is False
        assert "Failed to write hooks.json" in result["error"]
        # The original hooks file should have been restored correctly
        assert hooks_file.read_text() == "{}"


def test_uninstall_copilot(tmp_path: Path) -> None:
    copilot_path = tmp_path / ".copilot"
    hooks_dir = copilot_path / "hooks"
    hooks_dir.mkdir(parents=True)
    dispatcher = hooks_dir / "hook_dispatcher.py"
    dispatcher.write_text("dummy")

    hooks_file = copilot_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "pre-commit": [{"command": "python hook_dispatcher.py"}],
                    "pre-push": [{"command": "other"}],
                }
            }
        )
    )

    result = uninstall_copilot(tmp_path)
    assert result["success"] is True
    assert "pre-commit" in result["hooks_removed"]
    assert str(dispatcher) in result["files_removed"]
    assert not dispatcher.exists()

    # Verify hooks.json updated correctly
    data = json.loads(hooks_file.read_text())
    assert "pre-push" in data["hooks"]
    assert "pre-commit" not in data["hooks"]


def test_uninstall_copilot_errors(tmp_path: Path) -> None:
    copilot_path = tmp_path / ".copilot"
    hooks_dir = copilot_path / "hooks"
    hooks_dir.mkdir(parents=True)
    dispatcher = hooks_dir / "hook_dispatcher.py"
    dispatcher.write_text("dummy")

    hooks_file = copilot_path / "hooks.json"
    hooks_file.write_text("{bad")

    with patch("pathlib.Path.unlink", side_effect=OSError("unlink err")):
        result = uninstall_copilot(tmp_path)
        # Failures are softly caught and logged
        assert result["success"] is True
        assert result["hooks_removed"] == []
        assert result["files_removed"] == []
