"""Discovering older history preserves the task's newest candidate."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.commits import auto_link_commits_async
from gobby.utils.daemon_git import GitOk

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_autolink_historical_commit_preserves_newer_candidate(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    older, newer = "b" * 10, "a" * 10
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Discover an older commit",
        validation_criteria="Newest candidate stays last.",
    )
    manager.link_commit(task.id, newer)
    history = (
        f"{older}|[gobby-#{task.seq_num}] older change\n"
        f"{newer}|[gobby-#{task.seq_num}] newer candidate\n"
    )
    with (
        patch(
            "gobby.tasks.commits.daemon_git.run",
            new=AsyncMock(return_value=GitOk("ok", ("git", "log"), history, "")),
        ),
        patch("gobby.tasks.commit_ownership.assert_auto_link_commit_available", new=AsyncMock()),
    ):
        result = await auto_link_commits_async(
            manager, task_id=task.id, cwd=tmp_path, project_name="gobby"
        )

    assert result.total_linked == 1
    assert manager.get_task(task.id).commits == [older, newer]
