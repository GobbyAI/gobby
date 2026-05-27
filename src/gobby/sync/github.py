"""GitHub sync service that orchestrates between gobby tasks and GitHub.

This service delegates all GitHub operations to the official GitHub MCP server
(@modelcontextprotocol/server-github), avoiding custom API client code.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from gobby.integrations.github import GitHubIntegration
from gobby.integrations.github_helper import parse_github_repo

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.storage.tasks import LocalTaskManager

__all__ = [
    "GitHubSyncService",
    "GitHubSyncError",
    "GitHubRateLimitError",
    "GitHubNotFoundError",
]

logger = logging.getLogger(__name__)


class GitHubSyncError(Exception):
    """Base exception for GitHub sync errors."""

    pass


class GitHubRateLimitError(GitHubSyncError):
    """Raised when GitHub API rate limit is exceeded.

    Attributes:
        reset_at: Unix timestamp when rate limit resets.
    """

    def __init__(self, message: str, reset_at: int | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class GitHubNotFoundError(GitHubSyncError):
    """Raised when a GitHub resource is not found.

    Attributes:
        resource: Type of resource (e.g., "issue", "repo", "pr").
        resource_id: Identifier of the missing resource.
    """

    def __init__(
        self,
        message: str,
        resource: str | None = None,
        resource_id: int | str | None = None,
    ) -> None:
        super().__init__(message)
        self.resource = resource
        self.resource_id = resource_id


class GitHubSyncService:
    """Service for syncing gobby tasks with GitHub issues and PRs.

    This service orchestrates bidirectional sync between gobby tasks and GitHub:
    - Import GitHub issues as gobby tasks
    - Sync task updates back to GitHub issues
    - Create PRs from completed tasks

    All GitHub operations are delegated to the official GitHub MCP server.

    Attributes:
        mcp_manager: MCPClientManager for accessing GitHub MCP server.
        task_manager: LocalTaskManager for gobby task CRUD.
        project_id: Gobby project ID for creating tasks.
        github_repo: Default GitHub repo in "owner/repo" format.
        github: GitHubIntegration instance for availability checks.
    """

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        task_manager: LocalTaskManager,
        project_id: str,
        github_repo: str | None = None,
    ) -> None:
        """Initialize GitHubSyncService.

        Args:
            mcp_manager: MCPClientManager for GitHub MCP server access.
            task_manager: LocalTaskManager for gobby task operations.
            project_id: Gobby project ID for creating tasks.
            github_repo: Default GitHub repo in "owner/repo" format.
        """
        self.mcp_manager = mcp_manager
        self.task_manager = task_manager
        self.project_id = project_id
        self.github_repo = github_repo
        self.github = GitHubIntegration(mcp_manager)

    def is_available(self) -> bool:
        """Check if GitHub MCP server is available.

        Returns:
            True if GitHub MCP server is available, False otherwise.
        """
        return self.github.is_available()

    async def import_github_issues(
        self,
        repo: str,
        labels: list[str] | None = None,
        state: str = "open",
    ) -> list[dict[str, Any]]:
        """Import GitHub issues as gobby tasks.

        Fetches issues from GitHub via the MCP server and creates corresponding
        gobby tasks with linked github_issue_number and github_repo fields.

        Args:
            repo: GitHub repo in "owner/repo" format.
            labels: Optional list of labels to filter issues.
            state: Issue state to filter ("open", "closed", "all").

        Returns:
            List of created task dictionaries.

        Raises:
            RuntimeError: If GitHub MCP server is unavailable.
        """
        self.github.require_available()

        # Call GitHub MCP to list issues
        args: dict[str, Any] = {"owner": repo.split("/")[0], "repo": repo.split("/")[1]}
        if labels:
            args["labels"] = ",".join(labels)
        if state:
            args["state"] = state

        result = await self.mcp_manager.call_tool(
            server_name="github",
            tool_name="list_issues",
            arguments=args,
        )

        issues = result.get("issues", [])
        imported = []
        updated = []

        for issue in issues:
            issue_number = issue.get("number")
            title = issue.get("title", "Untitled Issue")
            description = issue.get("body", "")
            issue_labels = [
                lbl["name"] if isinstance(lbl, dict) else lbl for lbl in (issue.get("labels") or [])
            ]

            # Dedup: check if task already exists for this repo + issue number
            existing = self._find_task_by_github_issue(repo, issue_number)
            if existing:
                self.task_manager.update_task(
                    existing.id,
                    title=title,
                    description=description,
                    labels=issue_labels or None,
                )
                refreshed = self.task_manager.get_task(existing.id)
                if refreshed:
                    updated.append(refreshed.to_dict())
            else:
                task = self.task_manager.create_task(
                    project_id=self.project_id,
                    title=title,
                    description=description,
                    github_issue_number=issue_number,
                    github_repo=repo,
                    labels=issue_labels or None,
                )
                imported.append(task.to_dict())

        all_tasks = imported + updated
        logger.info(f"GitHub import from {repo}: {len(imported)} imported, {len(updated)} updated")
        return all_tasks

    def _find_task_by_github_issue(self, repo: str, issue_number: int | None) -> Any | None:
        """Find an existing task linked to a GitHub issue.

        Args:
            repo: GitHub repo in "owner/repo" format.
            issue_number: GitHub issue number.

        Returns:
            Task object if found, None otherwise.
        """
        if issue_number is None:
            return None
        row = self.task_manager.db.execute(
            "SELECT id FROM tasks WHERE github_repo = %s AND github_issue_number = %s "
            "AND project_id = %s LIMIT 1",
            (repo, issue_number, self.project_id),
        ).fetchone()
        if row:
            return self.task_manager.get_task(row["id"])
        return None

    async def sync_task_to_github(self, task_id: str) -> dict[str, Any]:
        """Sync a gobby task to its linked GitHub issue.

        Updates the GitHub issue title and body to match the task.

        Args:
            task_id: ID of the task to sync.

        Returns:
            Result from GitHub MCP update_issue call.

        Raises:
            RuntimeError: If GitHub MCP server is unavailable.
            ValueError: If task has no linked GitHub issue.
        """
        self.github.require_available()

        task = self.task_manager.get_task(task_id)

        if not task.github_issue_number:
            raise ValueError(
                f"Task {task_id} has no linked GitHub issue. Set github_issue_number to sync."
            )

        repo = task.github_repo or self.github_repo
        if not repo:
            raise ValueError(
                f"Task {task_id} has no github_repo set and no default repo configured."
            )

        owner, repo_name = parse_github_repo(repo)

        result = await self.mcp_manager.call_tool(
            server_name="github",
            tool_name="update_issue",
            arguments={
                "owner": owner,
                "repo": repo_name,
                "issue_number": task.github_issue_number,
                "title": task.title,
                "body": task.description or "",
            },
        )

        # Validate response
        if result is None or not isinstance(result, dict):
            raise GitHubSyncError(
                f"Invalid response from GitHub MCP when updating issue "
                f"#{task.github_issue_number}: expected dict, got {type(result).__name__}"
            )

        logger.info(f"Synced task {task_id} to GitHub issue #{task.github_issue_number}")
        return cast(dict[str, Any], result)

    async def create_pr_for_task(
        self,
        task_id: str,
        head_branch: str,
        base_branch: str = "main",
        draft: bool = False,
    ) -> dict[str, Any]:
        """Create a GitHub PR for a task.

        Creates a pull request on GitHub and links it to the task.

        Args:
            task_id: ID of the task to create PR for.
            head_branch: Branch containing the changes.
            base_branch: Branch to merge into (default: "main").
            draft: Whether to create as draft PR.

        Returns:
            Result from GitHub MCP create_pull_request call.

        Raises:
            RuntimeError: If GitHub MCP server is unavailable.
        """
        self.github.require_available()

        task = self.task_manager.get_task(task_id)

        repo = task.github_repo or self.github_repo
        if not repo:
            raise ValueError(
                f"Task {task_id} has no github_repo set and no default repo configured."
            )

        owner, repo_name = parse_github_repo(repo)

        # Create PR via GitHub MCP
        result = await self.mcp_manager.call_tool(
            server_name="github",
            tool_name="create_pull_request",
            arguments={
                "owner": owner,
                "repo": repo_name,
                "title": task.title,
                "body": task.description or "",
                "head": head_branch,
                "base": base_branch,
                "draft": draft,
            },
        )

        # Update task with PR number if available
        result_dict = cast(dict[str, Any], result)
        pr_number = result_dict.get("number")
        if pr_number:
            self.task_manager.update_task(
                task_id,
                github_pr_number=pr_number,
                github_repo=repo,
            )
            logger.info(f"Created PR #{pr_number} for task {task_id}")

        return result_dict

    def map_gobby_labels_to_github(
        self,
        gobby_labels: list[str],
        prefix: str = "",
    ) -> list[str]:
        """Map gobby labels to GitHub label format.

        Args:
            gobby_labels: List of gobby label strings.
            prefix: Optional prefix to add to each label.

        Returns:
            List of GitHub-formatted labels.
        """
        if not gobby_labels:
            return []

        github_labels = []
        for label in gobby_labels:
            if prefix:
                github_labels.append(f"{prefix}{label}")
            else:
                github_labels.append(label)

        return github_labels

    async def push_files_to_remote(
        self,
        branch: str,
        files: list[dict[str, str]],
        message: str,
    ) -> dict[str, Any]:
        """Push files directly to a remote branch via GitHub MCP.

        Creates a commit on the remote without requiring local git operations.
        Useful for automated JSONL sync export, config updates, etc.

        Args:
            branch: Target branch name.
            files: List of dicts with 'path' and 'content' keys.
            message: Commit message.

        Returns:
            Result from GitHub MCP push_files call.

        Raises:
            RuntimeError: If GitHub MCP server is unavailable.
            ValueError: If no github_repo configured.
        """
        self.github.require_available()

        repo = self.github_repo
        if not repo:
            raise ValueError("No github_repo configured for push_files_to_remote.")

        owner, repo_name = parse_github_repo(repo)

        result = await self.mcp_manager.call_tool(
            server_name="github",
            tool_name="push_files",
            arguments={
                "owner": owner,
                "repo": repo_name,
                "branch": branch,
                "files": files,
                "message": message,
            },
        )

        result_dict = cast(dict[str, Any], result)
        logger.info(f"Pushed {len(files)} files to {repo}:{branch}")
        return result_dict

    def map_github_labels_to_gobby(
        self,
        github_labels: list[str],
        strip_prefix: str = "",
    ) -> list[str]:
        """Map GitHub labels to gobby label format.

        Args:
            github_labels: List of GitHub label strings.
            strip_prefix: Optional prefix to strip from each label.

        Returns:
            List of gobby-formatted labels.
        """
        if not github_labels:
            return []

        gobby_labels = []
        for label in github_labels:
            if strip_prefix and label.startswith(strip_prefix):
                gobby_labels.append(label[len(strip_prefix) :])
            else:
                gobby_labels.append(label)

        return gobby_labels
