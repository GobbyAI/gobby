"""Machine-local JSONL backup path behavior."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.memory.export import _default_backup_path
from gobby.cli.tasks._utils.config import get_backup_manager
from gobby.config.persistence import MemoryBackupConfig
from gobby.sync.memories import MemoryBackupManager
from gobby.sync.tasks import TaskBackupError, TaskBackupManager

pytestmark = pytest.mark.unit


def test_task_backup_defaults_to_project_scoped_gobby_home(tmp_path: Path) -> None:
    task_manager = MagicMock()
    task_manager.db = MagicMock()

    with patch("gobby.paths.get_gobby_home", return_value=tmp_path):
        backup_manager = TaskBackupManager(task_manager)

        path = backup_manager._get_backup_path("project-123")

    assert path == tmp_path / "backups" / "project-123" / "tasks.jsonl"


def test_task_backup_default_requires_project_id() -> None:
    task_manager = MagicMock()
    task_manager.db = MagicMock()
    backup_manager = TaskBackupManager(task_manager)

    with pytest.raises(TaskBackupError, match="project_id is required"):
        backup_manager._get_backup_path(None)


def test_memory_backup_defaults_to_project_scoped_gobby_home(tmp_path: Path) -> None:
    with patch("gobby.paths.get_gobby_home", return_value=tmp_path):
        backup_manager = MemoryBackupManager(
            db=MagicMock(), memory_manager=MagicMock(), config=MemoryBackupConfig()
        )

        path = backup_manager._get_backup_path("project-123")

    assert path == tmp_path / "backups" / "project-123" / "memories.jsonl"


@patch("gobby.cli.tasks._utils.config.get_task_manager")
def test_task_cli_default_keeps_machine_local_path_resolution(
    get_task_manager: MagicMock,
) -> None:
    get_task_manager.return_value.db = MagicMock()

    backup_manager = get_backup_manager()

    assert backup_manager._custom_backup_path is False


def test_memory_cli_default_uses_project_uuid_directory(tmp_path: Path) -> None:
    project_ctx: dict[str, object] = {
        "id": "project-123",
        "project_path": str(tmp_path / "checkout"),
    }

    with patch("gobby.paths.get_gobby_home", return_value=tmp_path):
        path = _default_backup_path(project_ctx)

    assert path == tmp_path / "backups" / "project-123" / "memories.jsonl"
