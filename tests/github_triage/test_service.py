from __future__ import annotations

import hmac
import json
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.github_triage.service import GitHubIssueTriageService, TriageWebhookError
from gobby.storage.github_triage import GitHubTriageConfig, GitHubTriageStore
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


class FakeGitHubMCP:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        assert server_name == "github"
        self.calls.append((tool_name, arguments))
        value = self.responses.get(tool_name, {})
        if callable(value):
            return value(arguments)
        return value

    def called(self, tool_name: str) -> list[dict[str, Any]]:
        return [arguments for name, arguments in self.calls if name == tool_name]


def _payload(
    *,
    action: str = "opened",
    repo: str = "owner/repo",
    issue_number: int = 42,
    title: str = "Crash on launch",
    labels: list[str] | None = None,
) -> bytes:
    return json.dumps(
        {
            "action": action,
            "repository": {"full_name": repo},
            "issue": {
                "number": issue_number,
                "title": title,
                "body": "Steps to reproduce",
                "state": "open",
                "labels": [{"name": label} for label in (labels or ["bug"])],
                "updated_at": "2026-05-03T00:00:00Z",
                "html_url": f"https://github.com/{repo}/issues/{issue_number}",
            },
        }
    ).encode()


def _headers(raw_body: bytes, *, secret: str = "webhook-secret", delivery: str = "d-1"):
    signature = hmac.new(secret.encode(), raw_body, sha256).hexdigest()
    return {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={signature}",
    }


def _enable_config(
    temp_db,
    project_id: str,
    *,
    secret: str = "webhook-secret",
    repo: str = "owner/repo",
) -> None:
    GitHubTriageStore(temp_db).upsert_config(
        GitHubTriageConfig(
            project_id=project_id,
            enabled=True,
            webhook_enabled=True,
            repositories=(repo,),
            webhook_secret_ref=secret,
        )
    )


def test_webhook_acceptance_validates_hmac_and_deduplicates(temp_db, sample_project) -> None:
    raw_body = _payload()
    _enable_config(temp_db, sample_project["id"])
    service = GitHubIssueTriageService(db=temp_db)

    accepted = service.accept_webhook_delivery(
        sample_project["id"],
        _headers(raw_body),
        raw_body,
    )
    duplicate = service.accept_webhook_delivery(
        sample_project["id"],
        _headers(raw_body),
        raw_body,
    )

    assert accepted.status == "pending"
    assert accepted.duplicate is False
    assert duplicate.status == "duplicate"
    assert duplicate.duplicate is True


def test_webhook_rejects_bad_signature(temp_db, sample_project) -> None:
    raw_body = _payload()
    _enable_config(temp_db, sample_project["id"])
    headers = _headers(raw_body)
    headers["X-Hub-Signature-256"] = "sha256=bad"
    service = GitHubIssueTriageService(db=temp_db)

    with pytest.raises(TriageWebhookError, match="Invalid GitHub webhook signature"):
        service.accept_webhook_delivery(sample_project["id"], headers, raw_body)


@pytest.mark.asyncio
async def test_triage_issue_implement_creates_task_comments_labels_and_audit(
    temp_db,
    sample_project,
) -> None:
    _enable_config(temp_db, sample_project["id"])
    github = FakeGitHubMCP()
    build_func = AsyncMock()
    service = GitHubIssueTriageService(
        db=temp_db,
        mcp_manager=github,
        build_func=build_func,
    )

    result = await service.triage_issue(
        sample_project["id"],
        "owner/repo",
        42,
        "webhook",
        issue_data=json.loads(_payload().decode())["issue"],
    )

    assert result["verdict"] == "implement"
    task = LocalTaskManager(temp_db).get_task(result["task_id"])
    assert task.github_repo == "owner/repo"
    assert task.github_issue_number == 42
    assert github.called("add_issue_comment")
    assert github.called("add_labels_to_issue")[0]["labels"] == ["gobby:accepted"]
    build_func.assert_awaited_once()

    record = GitHubTriageStore(temp_db).get_issue_record(sample_project["id"], "owner/repo", 42)
    assert record is not None
    assert record.task_id == task.id
    assert record.verdict == "implement"


@pytest.mark.asyncio
async def test_triage_issue_dedup_closes_duplicate_without_task(temp_db, sample_project) -> None:
    _enable_config(temp_db, sample_project["id"])
    vector_store = AsyncMock()
    vector_store.search_with_payload.return_value = [
        (
            "duplicate",
            0.94,
            {
                "repo": "owner/other",
                "issue_number": 7,
                "issue_url": "https://github.com/owner/other/issues/7",
            },
        )
    ]
    memory_manager = SimpleNamespace(
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[0.1, 0.2]),
    )
    github = FakeGitHubMCP()
    service = GitHubIssueTriageService(
        db=temp_db,
        mcp_manager=github,
        memory_manager=memory_manager,
        build_func=AsyncMock(),
    )

    result = await service.triage_issue(
        sample_project["id"],
        "owner/repo",
        42,
        "webhook",
        issue_data=json.loads(_payload().decode())["issue"],
    )

    assert result["verdict"] == "dedup"
    assert result["task_id"] is None
    assert github.called("add_labels_to_issue")[0]["labels"] == ["gobby:duplicate"]
    assert github.called("update_issue")[0]["state"] == "closed"
    record = GitHubTriageStore(temp_db).get_issue_record(sample_project["id"], "owner/repo", 42)
    assert record is not None
    assert record.dedup_issue_key == "owner/other#7"


@pytest.mark.asyncio
async def test_reconcile_project_repos_lists_open_issues_and_triages(
    temp_db,
    sample_project,
) -> None:
    _enable_config(temp_db, sample_project["id"])
    github = FakeGitHubMCP(
        {
            "list_issues": {
                "issues": [
                    json.loads(_payload(issue_number=1, title="First").decode())["issue"],
                    json.loads(_payload(issue_number=2, title="Second").decode())["issue"],
                ]
            }
        }
    )
    service = GitHubIssueTriageService(
        db=temp_db,
        mcp_manager=github,
        build_func=AsyncMock(),
    )

    result = await service.reconcile_project_repos(sample_project["id"])

    assert result == {"scanned": 2, "triaged": 2, "errors": 0}
    assert github.called("list_issues")[0]["state"] == "open"
    assert github.called("list_issues")[0]["page"] == 1


@pytest.mark.asyncio
async def test_close_linked_issue_after_merge_comments_labels_and_closes(
    temp_db,
    sample_project,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Linked issue",
        github_repo="owner/repo",
        github_issue_number=99,
    )
    github = FakeGitHubMCP()
    service = GitHubIssueTriageService(db=temp_db, mcp_manager=github)

    closed = await service.close_linked_issue_after_merge(task.id, "abc123")

    assert closed is True
    assert "abc123" in github.called("add_issue_comment")[0]["body"]
    assert github.called("add_labels_to_issue")[0]["labels"] == ["gobby:resolved"]
    assert github.called("update_issue")[0]["state"] == "closed"
