"""Owner-controlled task path release tool tests."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.mark.asyncio
async def test_release_task_paths_is_owner_only_and_clears_commit_guard_attribution(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    project = LocalProjectManager(temp_db).create(
        "release-task-paths-test",
        repo_path=str(repo),
    )
    sessions = SessionManager(temp_db)
    owner = sessions.register(
        external_id="release-owner",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
    )
    foreign = sessions.register(
        external_id="release-foreign",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project.id,
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=project.id,
        title="Release committed task path",
        task_type="bug",
        category="code",
        implementation_domain="backend",
        validation_criteria="The owner can release stale task path attribution.",
        claimed_by_session_id=owner.id,
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        owner.id,
        {
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "active_task_id": task.id,
            "task_edited_files": {task.id: ["src/committed.py", "src/in-flight.py"]},
        },
    )
    registry = create_task_registry(tasks)

    with session_context_for_test(foreign.id):
        rejected = await registry.call(
            "release_task_paths",
            {"task_id": task.id, "paths": ["src/committed.py"]},
        )

    assert rejected == {
        "success": False,
        "status": "error",
        "error": "Only the task's owning session can release attributed paths",
        "error_code": "TASK_CLAIM_CONFLICT",
        "task_id": task.id,
        "owner_session_id": owner.id,
        "session_id": foreign.id,
    }
    assert variables.get_variables(owner.id)["task_edited_files"][task.id] == [
        "src/committed.py",
        "src/in-flight.py",
    ]

    with session_context_for_test(owner.id):
        invalid = await registry.call(
            "release_task_paths",
            {"task_id": task.id, "paths": ["../outside.py"]},
        )

    assert invalid == {
        "success": False,
        "status": "error",
        "error": "Invalid repository-relative path: '../outside.py'",
        "error_code": "TASK_INVALID_STATUS",
    }

    with session_context_for_test(owner.id):
        released = await registry.call(
            "release_task_paths",
            {"task_id": task.id, "paths": ["./src/committed.py"]},
        )

    assert released == {
        "success": True,
        "task_id": task.id,
        "released_paths": ["src/committed.py"],
        "remaining_paths": ["src/in-flight.py"],
    }
    assert variables.get_variables(owner.id)["task_edited_files"][task.id] == ["src/in-flight.py"]

    assert "release_task_paths" in {item["name"] for item in registry.list_tools()}
