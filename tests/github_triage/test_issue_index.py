from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gobby.github_triage.issue_index import (
    GITHUB_ISSUE_COLLECTION,
    GitHubIssueIndexer,
    IssueSnapshot,
    build_issue_content,
    content_hash,
    issue_point_id,
)
from gobby.projects.fenced_vector_store import ProjectFencedVectorStore
from gobby.projects.write_fence import ProjectWriteFence
from tests.projects.fence_helpers import wait_for_exclusive_claim

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
    assert content_hash(issue) != content_hash(_issue(body="Different reproduction"))
    assert content_hash(issue) != content_hash(_issue(labels=("bug", "p1", "customer")))


def test_content_hash_ignores_gobby_managed_labels_and_update_timestamp() -> None:
    issue = _issue()

    assert content_hash(issue) == content_hash(
        _issue(
            labels=("p1", "gobby:accepted", "bug", "gobby:needs-triage"),
            updated_at="2026-05-04T00:00:00Z",
        )
    )


def test_build_issue_content_omits_labels() -> None:
    content = build_issue_content(_issue(labels=("bug", "p1", "support")))

    assert "Labels:" not in content
    assert "support" not in content


def test_issue_snapshot_from_github_rejects_invalid_number() -> None:
    with pytest.raises(ValueError, match="valid integer number"):
        IssueSnapshot.from_github(
            project_id="project-1",
            repo="owner/repo",
            issue={"number": "not-an-int", "title": "Bad"},
        )


@pytest.mark.asyncio
async def test_upsert_uses_dedicated_collection_and_payload() -> None:
    vector_store = AsyncMock()
    embed_fn = AsyncMock(return_value=[0.1, 0.2])
    indexer = GitHubIssueIndexer(vector_store=vector_store, embed_fn=embed_fn)
    issue = _issue()

    point_id = await indexer.upsert(issue, task_id="task-1")

    assert point_id == issue_point_id("project-1", "owner/repo", 42)
    vector_store.ensure_collection.assert_awaited_once_with(
        GITHUB_ISSUE_COLLECTION,
        recreate_on_mismatch=True,
    )
    vector_store.upsert.assert_awaited_once()
    args, kwargs = vector_store.upsert.await_args
    assert args[0] == point_id
    assert args[1] == [0.1, 0.2]
    embed_fn.assert_awaited_once_with(build_issue_content(issue))
    assert args[2]["project_id"] == "project-1"
    assert args[2]["task_id"] == "task-1"
    assert args[2]["source_text"] == build_issue_content(issue)
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
        ("weak", 0.94, {"repo": "owner/third", "issue_number": 9}),
    ]
    embed_fn = AsyncMock(return_value=[0.1, 0.2])
    indexer = GitHubIssueIndexer(vector_store=vector_store, embed_fn=embed_fn)

    duplicates = await indexer.find_duplicates(_issue())

    vector_store.ensure_collection.assert_awaited_once_with(
        GITHUB_ISSUE_COLLECTION,
        recreate_on_mismatch=True,
    )
    vector_store.search_with_payload.assert_awaited_once()
    _, kwargs = vector_store.search_with_payload.await_args
    assert kwargs["filters"] == {"project_id": "project-1"}
    assert kwargs["collection_name"] == GITHUB_ISSUE_COLLECTION
    assert [duplicate.issue_key for duplicate in duplicates] == ["owner/other#5"]
    assert duplicates[0].task_id == "task-5"


@pytest.mark.asyncio
async def test_find_duplicates_uses_high_confidence_default_threshold() -> None:
    vector_store = AsyncMock()
    vector_store.search_with_payload.return_value = [
        ("uncertain", 0.94, {"repo": "owner/near", "issue_number": 9}),
    ]
    embed_fn = AsyncMock(return_value=[0.1, 0.2])
    indexer = GitHubIssueIndexer(vector_store=vector_store, embed_fn=embed_fn)

    assert await indexer.find_duplicates(_issue()) == []


@pytest.mark.asyncio
async def test_find_duplicates_warns_and_degrades_when_vector_search_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    vector_store = AsyncMock()
    vector_store.search_with_payload.side_effect = RuntimeError("qdrant unavailable")
    embed_fn = AsyncMock(return_value=[0.1, 0.2])
    indexer = GitHubIssueIndexer(vector_store=vector_store, embed_fn=embed_fn)

    duplicates = await indexer.find_duplicates(_issue())

    assert duplicates == []
    assert "vector duplicate search failed" in caplog.text
    assert "owner/repo#42" in caplog.text


@pytest.mark.asyncio
async def test_issue_index_holds_project_admission_from_embedding_through_upsert() -> None:
    project = SimpleNamespace(deleted_at=None)
    fence = ProjectWriteFence(lambda _project_id: project)
    inner = AsyncMock()
    embed_started = asyncio.Event()
    release_embed = asyncio.Event()

    async def embed(_content: str) -> list[float]:
        embed_started.set()
        await release_embed.wait()
        return [0.1, 0.2]

    vector_store = ProjectFencedVectorStore(inner, fence)
    indexer = GitHubIssueIndexer(vector_store=vector_store, embed_fn=embed)
    write_task = asyncio.create_task(indexer.upsert(_issue()))
    await embed_started.wait()
    project.deleted_at = object()
    exclusive_entered = asyncio.Event()

    async def purge() -> None:
        async with fence.exclusive("project-1", timeout=1.0):
            exclusive_entered.set()

    purge_task = asyncio.create_task(purge())
    await wait_for_exclusive_claim(fence, "project-1")
    assert not exclusive_entered.is_set()

    release_embed.set()
    assert await write_task == issue_point_id("project-1", "owner/repo", 42)
    await purge_task
    assert exclusive_entered.is_set()
