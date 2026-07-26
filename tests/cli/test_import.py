import json
import logging
import subprocess
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.sync.task_github_import import GITHUB_CLI_TIMEOUT_SECONDS, GitHubIssueImporter

pytestmark = pytest.mark.unit

# projects.id is a native uuid column.
PROJECT_ID = "aeaeaeae-0000-4000-8000-000000000001"
RESOLUTION_PROJECT_ID = "aeaeaeae-0000-4000-8000-000000000002"

# import_from_github_issues derives project-scoped deterministic uuid5 task ids
# from normalized owner/repo/issues/<number>; re-imports upsert in place.


@pytest.fixture
def github_importer(hub_db: HubDatabase) -> GitHubIssueImporter:
    return GitHubIssueImporter(hub_db)


@pytest.fixture
def github_project_id(hub_db: HubDatabase) -> str:
    hub_db.execute(
        "INSERT INTO projects (id, repo_path, name, github_url) VALUES (%s, %s, %s, %s)",
        (
            PROJECT_ID,
            "/tmp/github-import-normalization",
            "GitHub Import Normalization",
            "https://github.com/owner/repo",
        ),
    )
    return PROJECT_ID


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_from_github_issues(
    github_importer: GitHubIssueImporter,
    hub_db: HubDatabase,
) -> None:
    # Setup project with matching URL
    hub_db.execute(
        "INSERT INTO projects (id, repo_path, name, github_url) VALUES (%s, %s, %s, %s)",
        (PROJECT_ID, "/tmp/test", "Test Project", "https://github.com/owner/repo"),
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

        result = await github_importer.import_from_github_issues("https://github.com/owner/repo")

        assert result["success"] is True
        assert len(result["imported"]) == 1
        assert result["imported"][0] == str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{PROJECT_ID}/github/owner/repo/issues/1",
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_project_id_resolution(
    github_importer: GitHubIssueImporter,
    hub_db: HubDatabase,
) -> None:
    """
    Test that import_from_github_issues correctly resolves the project_id
    from the database based on the repo URL, without needing claude_agent_sdk.
    """
    # Setup: Insert a project with a known GitHub URL
    repo_url = "https://github.com/test/resolution"
    expected_project_id = RESOLUTION_PROJECT_ID
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
        result = await github_importer.import_from_github_issues(repo_url)

    # Assert
    assert result["success"] is True
    assert result["count"] == 1

    # Verify the task was created with the correct project_id
    imported_id = result["imported"][0]
    row = hub_db.fetchone("SELECT project_id FROM tasks WHERE id = %s", (imported_id,))
    assert row is not None
    assert row["project_id"] == expected_project_id


@pytest.mark.asyncio
async def test_import_runs_database_upserts_in_worker_thread(
    github_importer: GitHubIssueImporter,
) -> None:
    issue = {
        "number": 7,
        "title": "Threaded import",
        "body": "Body",
        "createdAt": "2023-01-01T00:00:00Z",
    }
    with (
        patch.object(
            github_importer,
            "_fetch_github_issues_mcp",
            new=AsyncMock(return_value=[issue]),
        ),
        patch(
            "gobby.sync.task_github_import.asyncio.to_thread",
            new=AsyncMock(return_value=(["task-id"], 1)),
        ) as mock_to_thread,
    ):
        result = await github_importer.import_from_github_issues(
            "https://github.com/owner/repo",
            project_id=PROJECT_ID,
        )

    assert result["success"] is True
    assert mock_to_thread.await_count == 1
    assert mock_to_thread.await_args is not None
    assert mock_to_thread.await_args.args[0] == github_importer._upsert_issues


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_skips_invalid_issue_numbers_with_warning_context(
    github_importer: GitHubIssueImporter,
    github_project_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid_numbers: list[object] = [None, "7", True, 0, -1, 2**31]
    issues = [
        {"number": issue_number, "title": f"Invalid {index}"}
        for index, issue_number in enumerate(invalid_numbers)
    ]
    issues.append({"number": 7, "title": "Valid issue"})

    with (
        caplog.at_level(logging.WARNING, logger="gobby.sync.task_github_import"),
        patch.object(
            github_importer,
            "_fetch_github_issues_mcp",
            new=AsyncMock(return_value=issues),
        ),
    ):
        result = await github_importer.import_from_github_issues(
            "https://github.com/owner/repo",
            project_id=github_project_id,
        )

    assert result["success"] is True
    assert result["count"] == 1
    assert len(result["imported"]) == 1
    skip_records = [
        record
        for record in caplog.records
        if record.getMessage() == "Skipping GitHub issue with invalid number"
    ]
    assert [record.__dict__["github_issue_number"] for record in skip_records] == invalid_numbers
    assert {record.__dict__["github_repo"] for record in skip_records} == {"owner/repo"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_normalizes_malformed_labels_and_timestamps(
    github_importer: GitHubIssueImporter,
    github_project_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issues = [
        {
            "number": 8,
            "title": "Mixed labels",
            "labels": [
                {"name": " bug "},
                "triage",
                {"name": None},
                {"color": "ffffff"},
                42,
                " ",
            ],
            "createdAt": "not-a-timestamp",
        },
        {
            "number": 9,
            "title": "Malformed fields",
            "labels": None,
            "created_at": {"unexpected": "object"},
        },
    ]
    started_at = datetime.now(UTC)

    with (
        caplog.at_level(logging.WARNING, logger="gobby.sync.task_github_import"),
        patch.object(
            github_importer,
            "_fetch_github_issues_mcp",
            new=AsyncMock(return_value=issues),
        ),
    ):
        result = await github_importer.import_from_github_issues(
            "https://github.com/owner/repo",
            project_id=github_project_id,
        )
    finished_at = datetime.now(UTC)

    assert result["success"] is True
    assert result["count"] == 2
    imported_tasks = [
        github_importer.task_manager.get_task(task_id) for task_id in result["imported"]
    ]
    assert [task.labels for task in imported_tasks] == [["bug", "triage"], []]
    assert all(started_at <= task.created_at <= finished_at for task in imported_tasks)
    timestamp_records = [
        record
        for record in caplog.records
        if record.getMessage()
        == "Using current time for GitHub issue with invalid creation timestamp"
    ]
    assert [record.__dict__["github_issue_number"] for record in timestamp_records] == [8, 9]
    assert {record.__dict__["github_repo"] for record in timestamp_records} == {"owner/repo"}


def test_github_cli_subprocess_timeouts_are_bounded(
    github_importer: GitHubIssueImporter,
) -> None:
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(
            ["gh", "--version"],
            timeout=GITHUB_CLI_TIMEOUT_SECONDS,
        ),
    ) as mock_run:
        assert github_importer._fetch_github_issues_cli("owner", "repo", "url", 50) is None
    assert mock_run.call_args.kwargs["timeout"] == GITHUB_CLI_TIMEOUT_SECONDS

    with patch(
        "subprocess.run",
        side_effect=[
            MagicMock(returncode=0),
            subprocess.TimeoutExpired(
                ["gh", "issue", "list"],
                timeout=GITHUB_CLI_TIMEOUT_SECONDS,
            ),
        ],
    ) as mock_run:
        with pytest.raises(
            RuntimeError,
            match=f"timed out after {GITHUB_CLI_TIMEOUT_SECONDS} seconds",
        ):
            github_importer._fetch_github_issues_cli("owner", "repo", "url", 50)
    assert mock_run.call_args_list[0].kwargs["timeout"] == GITHUB_CLI_TIMEOUT_SECONDS
    assert mock_run.call_args_list[1].kwargs["timeout"] == GITHUB_CLI_TIMEOUT_SECONDS
