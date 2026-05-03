from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gobby.github_triage.issue_index import (
    GITHUB_ISSUE_COLLECTION,
    GitHubIssueIndexer,
    IssueSnapshot,
    content_hash,
    issue_point_id,
)

pytestmark = pytest.mark.unit


def _issue(**overrides) -> IssueSnapshot:
    values = {
        "project_id": "project-1",
        "repo": "owner/repo",
        "issue_number": 42,
        "title": "Crash on launch",
        "body": "Steps to reproduce",
        "state": "open",
        "labels": ("bug", "p1"),
        "updated_at": "2026-05-03T00:00:00Z",
        "issue_url": "https://github.com/owner/repo/issues/42",
    }
    values.update(overrides)
    return IssueSnapshot(**values)


def test_point_id_is_deterministic_for_project_repo_issue() -> None:
    assert issue_point_id("project-1", "owner/repo", 42) == issue_point_id(
        "project-1",
        "owner/repo",
        42,
    )
    assert issue_point_id("project-1", "owner/repo", 42) != issue_point_id(
        "project-1",
        "owner/repo",
        43,
    )


def test_content_hash_changes_when_triage_relevant_content_changes() -> None:
    issue = _issue()

    assert content_hash(issue) == content_hash(_issue(labels=("bug", "p1")))
    assert content_hash(issue) != content_hash(_issue(title="Crash after login"))


@pytest.mark.asyncio
async def test_upsert_uses_dedicated_collection_and_payload() -> None:
    vector_store = AsyncMock()
    embed_fn = AsyncMock(return_value=[0.1, 0.2])
    indexer = GitHubIssueIndexer(vector_store=vector_store, embed_fn=embed_fn)
    issue = _issue()

    point_id = await indexer.upsert(issue, task_id="task-1")

    assert point_id == issue_point_id("project-1", "owner/repo", 42)
    vector_store.ensure_collection.assert_awaited_once_with(GITHUB_ISSUE_COLLECTION)
    vector_store.upsert.assert_awaited_once()
    args, kwargs = vector_store.upsert.await_args
    assert args[0] == point_id
    assert args[1] == [0.1, 0.2]
    assert args[2]["project_id"] == "project-1"
    assert args[2]["task_id"] == "task-1"
    assert kwargs["collection_name"] == GITHUB_ISSUE_COLLECTION


@pytest.mark.asyncio
async def test_find_duplicates_is_project_scoped_and_skips_self() -> None:
    vector_store = AsyncMock()
    vector_store.search_with_payload.return_value = [
        (
            "self",
            0.99,
            {"repo": "owner/repo", "issue_number": 42, "issue_url": None},
        ),
        (
            "match",
            0.95,
            {
                "repo": "owner/other",
                "issue_number": 5,
                "issue_url": "https://github.com/owner/other/issues/5",
                "task_id": "task-5",
            },
        ),
        ("weak", 0.50, {"repo": "owner/third", "issue_number": 9}),
    ]
    embed_fn = AsyncMock(return_value=[0.1, 0.2])
    indexer = GitHubIssueIndexer(vector_store=vector_store, embed_fn=embed_fn)

    duplicates = await indexer.find_duplicates(_issue())

    vector_store.search_with_payload.assert_awaited_once()
    _, kwargs = vector_store.search_with_payload.await_args
    assert kwargs["filters"] == {"project_id": "project-1"}
    assert kwargs["collection_name"] == GITHUB_ISSUE_COLLECTION
    assert [duplicate.issue_key for duplicate in duplicates] == ["owner/other#5"]
    assert duplicates[0].task_id == "task-5"
