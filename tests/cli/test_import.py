import json
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager

pytestmark = pytest.mark.unit


@pytest.fixture
def sync_manager(hub_db):
    tm = LocalTaskManager(hub_db)
    return TaskSyncManager(tm, export_path=".gobby/tasks.jsonl")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_from_github_issues(sync_manager, hub_db):
    # Setup project with matching URL
    hub_db.execute(
        "INSERT INTO projects (id, repo_path, name, github_url) VALUES (%s, %s, %s, %s)",
        ("proj-123", "/tmp/test", "Test Project", "https://github.com/owner/repo"),
    )

    with patch("subprocess.run") as mock_run:
        # Mock gh --version
        mock_run.side_effect = [
            MagicMock(returncode=0),  # gh --version
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "number": 1,
                            "title": "Issue 1",
                            "body": "Desc 1",
                            "labels": [{"name": "bug"}],
                            "createdAt": "2023-01-01T00:00:00Z",
                        }
                    ]
                ),
            ),  # gh issue list
        ]

        result = await sync_manager.import_from_github_issues("https://github.com/owner/repo")

        assert result["success"] is True
        assert len(result["imported"]) == 1
        assert "gh-1" in result["imported"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_project_id_resolution(sync_manager, hub_db):
    """
    Test that import_from_github_issues correctly resolves the project_id
    from the database based on the repo URL, without needing claude_agent_sdk.
    """
    # Setup: Insert a project with a known GitHub URL
    repo_url = "https://github.com/test/resolution"
    expected_project_id = "proj-resolution-test"
    hub_db.execute(
        "INSERT INTO projects (id, repo_path, name, github_url) VALUES (%s, %s, %s, %s)",
        (expected_project_id, "/tmp/resolution", "Resolution Project", repo_url),
    )

    # Mock subprocess.run to return a dummy issue
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # gh version check
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "number": 101,
                            "title": "Resolved Issue",
                            "body": "Body",
                            "createdAt": "2023-01-01T00:00:00Z",
                        }
                    ]
                ),
            ),  # gh issue list
        ]

        # Act: Import without specifying project_id
        result = await sync_manager.import_from_github_issues(repo_url)

    # Assert
    assert result["success"] is True
    assert result["count"] == 1

    # Verify the task was created with the correct project_id
    row = hub_db.fetchone("SELECT project_id FROM tasks WHERE id = %s", ("gh-101",))
    assert row["project_id"] == expected_project_id
