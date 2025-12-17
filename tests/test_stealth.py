import pytest
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
from gobby.cli.tasks import get_sync_manager


@pytest.fixture
def mock_stealth_config(tmp_path):
    ctx = {
        "id": "test_project_123",
        "name": "Test Project",
        "project_path": str(tmp_path),
        "tasks_stealth": True,
    }
    return ctx


@pytest.fixture
def mock_normal_config(tmp_path):
    ctx = {
        "id": "test_project_123",
        "name": "Test Project",
        "project_path": str(tmp_path),
        "tasks_stealth": False,
    }
    return ctx


@patch("gobby.cli.tasks.get_project_context")
@patch("gobby.cli.tasks.get_task_manager")
def test_stealth_mode_enabled(mock_tm, mock_ctx, mock_stealth_config):
    mock_ctx.return_value = mock_stealth_config
    mock_tm.return_value = MagicMock()

    # We need to mock Path.home to point to a temporary location to avoid messing with real home
    # But since we're just checking the path string, maybe okay.
    # Actually, the code calls mkdir on home/.gobby...

    with patch("pathlib.Path.home") as mock_home:
        temp_home = Path("/tmp/mock_home")
        mock_home.return_value = temp_home

        manager = get_sync_manager()

        expected_path = str(temp_home / ".gobby" / "stealth_tasks" / "test_project_123.jsonl")
        assert manager.export_path == Path(expected_path)


@patch("gobby.cli.tasks.get_project_context")
@patch("gobby.cli.tasks.get_task_manager")
def test_stealth_mode_disabled(mock_tm, mock_ctx, mock_normal_config):
    mock_ctx.return_value = mock_normal_config
    mock_tm.return_value = MagicMock()

    manager = get_sync_manager()

    assert str(manager.export_path) == ".gobby/tasks.jsonl"
