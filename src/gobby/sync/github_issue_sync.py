"""GitHub issue synchronization kept separate from automated triage."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import psycopg

from gobby.build.delivery import resolve_project_source_repo
from gobby.integrations.github_helper import parse_github_mcp_result, parse_github_repo
from gobby.storage.github_triage import GitHubTriageConfig, GitHubTriageStore
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.github import GitHubSyncService
from gobby.sync.github_validation import (
    is_github_rate_limit_error,
    normalize_github_issue_number,
)
from gobby.tasks.import_criteria import external_issue_validation_criteria
from gobby.utils.datetime import parse_stored_datetime

logger = logging.getLogger(__name__)

TriageIssueCallback = Callable[..., Awaitable[dict[str, Any]]]
_GITHUB_PAGE_SIZE = 100
_MAX_GITHUB_RECOVERY_PAGES = 100


class GitHubRepositoryReadinessError(RuntimeError):
    """A configured repository cannot be resolved or accessed."""


class GitHubIssueSyncService:
    """Synchronize linked GitHub issues without invoking triage automation."""

    def __init__(
        self,
        *,
        db: Any,
        mcp_manager: Any,
        task_manager: LocalTaskManager | None = None,
        project_manager: LocalProjectManager | None = None,
    ) -> None:
        self.db = db
        self.mcp_manager = mcp_manager
        self.task_manager = task_manager or LocalTaskManager(db)
        self.project_manager = project_manager or LocalProjectManager(db)
        self.config_store = GitHubTriageStore(db)

    def repositories_for(self, project: Project, config: GitHubTriageConfig) -> tuple[str, ...]:
        """Resolve configured repos, then the project's canonical GitHub repo."""
        if config.repositories:
            return config.repositories
        return (resolve_project_source_repo(self.db, project.id),)

    async def check_access(self, project: Project, config: GitHubTriageConfig) -> tuple[str, ...]:
        """Verify the connector can read every configured repository."""
        try:
            repositories = await asyncio.to_thread(self.repositories_for, project, config)
        except ValueError as exc:
            raise GitHubRepositoryReadinessError(
                "GitHub repository is unresolved; configure github_repo, github_url, or a git origin"
            ) from exc
        for repo in repositories:
            try:
                owner, repo_name = parse_github_repo(repo)
                await self._call(
                    "list_issues",
                    {
                        "owner": owner,
                        "repo": repo_name,
                        "state": "all",
                        "page": 1,
                        "per_page": 1,
                    },
                )
            except Exception as exc:
                error = GitHubRepositoryReadinessError(
                    f"GitHub connector cannot access repository {repo}: {exc}"
                )
                _copy_retry_metadata(exc, error)
                raise error from exc
        return repositories

    async def sync_issue(
        self,
        project_id: str,
        repo: str,
        issue_number: int,
        *,
        issue_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the one Gobby task linked to a GitHub issue."""
        normalized_issue_number = normalize_github_issue_number(issue_number)
        if normalized_issue_number is None:
            raise ValueError("GitHub issue number must be a positive integer")
        issue_number = normalized_issue_number

        project = await asyncio.to_thread(self.project_manager.get, project_id)
        if project is None or project.deleted_at:
            raise ValueError(f"Unknown project: {project_id}")
        config = await asyncio.to_thread(self.config_store.get_config, project_id)
        if not (config.sync_enabled or config.triage_enabled):
            return {"action": "disabled"}
        repositories = await asyncio.to_thread(self.repositories_for, project, config)
        if repo not in repositories:
            raise ValueError(f"Repository {repo!r} is not enabled for GitHub issue sync")

        issue = issue_data or await self._fetch_issue(repo, issue_number)
        if issue.get("pull_request"):
            return {"action": "skipped_pull_request"}
        response_number = normalize_github_issue_number(issue.get("number", issue_number))
        if response_number != issue_number:
            raise ValueError("GitHub issue response number does not match the requested issue")

        existing = await asyncio.to_thread(
            self.db.fetchone,
            "SELECT id, title, description, labels, validation_criteria, updated_at FROM tasks "
            "WHERE project_id = %s AND github_repo = %s AND github_issue_number = %s",
            (project_id, repo, issue_number),
        )
        updates = self._issue_updates(issue)
        validation_criteria = external_issue_validation_criteria(
            "GitHub",
            f"{repo}#{issue_number}",
        )
        if existing:
            remote_updated = parse_stored_datetime(issue.get("updated_at"))
            local_updated = parse_stored_datetime(existing.get("updated_at"))
            if local_updated and remote_updated and local_updated > remote_updated:
                return {"action": "skipped_local_newer", "task_id": existing["id"]}
            existing_labels = list(existing.get("labels") or [])
            updates["labels"] = list(dict.fromkeys([*existing_labels, *updates["labels"]]))
            metadata_updates: dict[str, Any] = {}
            if updates["labels"] != existing_labels:
                metadata_updates["labels"] = updates["labels"]
            if existing.get("validation_criteria") != validation_criteria:
                metadata_updates["validation_criteria"] = validation_criteria
            if metadata_updates:
                await asyncio.to_thread(
                    self.task_manager.update_task,
                    existing["id"],
                    **metadata_updates,
                )
            updates.pop("labels")
            task = await asyncio.to_thread(
                self.task_manager.reconcile_task_state,
                existing["id"],
                **updates,
            )
            return {"action": "updated", "task_id": task.id}

        try:
            task = await asyncio.to_thread(
                self.task_manager.create_task,
                project_id=project_id,
                title=updates["title"],
                description=updates["description"],
                labels=updates["labels"],
                github_issue_number=issue_number,
                github_repo=repo,
                validation_criteria=validation_criteria,
            )
        except psycopg.IntegrityError:
            existing = await asyncio.to_thread(
                self.db.fetchone,
                "SELECT id FROM tasks WHERE project_id = %s AND github_repo = %s "
                "AND github_issue_number = %s",
                (project_id, repo, issue_number),
            )
            if not existing:
                raise
            await asyncio.to_thread(
                self.task_manager.update_task,
                existing["id"],
                validation_criteria=validation_criteria,
            )
            task = await asyncio.to_thread(
                self.task_manager.reconcile_task_state,
                existing["id"],
                **updates,
            )
            return {"action": "updated", "task_id": task.id}
        lifecycle = {
            key: value
            for key, value in updates.items()
            if key not in {"title", "description", "labels"}
        }
        if lifecycle:
            task = await asyncio.to_thread(
                self.task_manager.reconcile_task_state,
                task.id,
                **lifecycle,
            )
        return {"action": "created", "task_id": task.id}

    async def recover_project(self, project_id: str) -> dict[str, int]:
        """Recover missed issue webhooks for all configured repositories."""
        project = await asyncio.to_thread(self.project_manager.get, project_id)
        if project is None or project.deleted_at:
            raise ValueError(f"Unknown project: {project_id}")
        config = await asyncio.to_thread(self.config_store.get_config, project_id)
        if not (config.sync_enabled or config.triage_enabled):
            return self._empty_stats()

        stats = self._empty_stats()
        repositories = await asyncio.to_thread(self.repositories_for, project, config)
        for repo in repositories:
            owner, repo_name = parse_github_repo(repo)
            seen_pages: set[tuple[str, ...]] = set()
            for page in range(1, _MAX_GITHUB_RECOVERY_PAGES + 1):
                try:
                    result = await self._call(
                        "list_issues",
                        {
                            "owner": owner,
                            "repo": repo_name,
                            "state": "all",
                            "page": page,
                            "per_page": _GITHUB_PAGE_SIZE,
                        },
                    )
                    issues = result.get("issues", []) if isinstance(result, dict) else result
                    if not isinstance(issues, list):
                        raise RuntimeError("GitHub list_issues returned an invalid payload")
                    page_marker = tuple(
                        repr(issue.get("number")) if isinstance(issue, dict) else repr(issue)
                        for issue in issues
                    )
                    if page_marker in seen_pages:
                        stats["errors"] += 1
                        break
                    seen_pages.add(page_marker)
                except Exception as exc:
                    if is_github_rate_limit_error(exc):
                        raise
                    stats["errors"] += 1
                    break
                for issue in issues:
                    if not isinstance(issue, dict) or issue.get("pull_request"):
                        continue
                    stats["scanned"] += 1
                    issue_number = normalize_github_issue_number(issue.get("number"))
                    if issue_number is None:
                        stats["errors"] += 1
                        continue
                    try:
                        action = (
                            await self.sync_issue(
                                project_id,
                                repo,
                                issue_number,
                                issue_data=issue,
                            )
                        )["action"]
                        if action in stats:
                            stats[action] += 1
                        else:
                            stats["skipped"] += 1
                    except Exception:
                        stats["errors"] += 1
                if len(issues) < _GITHUB_PAGE_SIZE:
                    break
        return stats

    async def push_linked_tasks(
        self,
        project_id: str,
        updated_after: datetime | None,
    ) -> dict[str, int]:
        """Push linked tasks only; local-native tasks never create GitHub issues."""
        project = await asyncio.to_thread(self.project_manager.get, project_id)
        if project is None or project.deleted_at:
            raise ValueError(f"Unknown project: {project_id}")
        query = (
            "SELECT id, github_repo, github_issue_number FROM tasks "
            "WHERE project_id = %s AND github_repo IS NOT NULL "
            "AND github_issue_number IS NOT NULL "
        )
        params: list[Any] = [project_id]
        if updated_after is not None:
            query += "AND updated_at > %s "
            params.append(updated_after)
        query += "ORDER BY updated_at, id"
        rows = await asyncio.to_thread(
            self.db.fetchall,
            query,
            tuple(params),
        )
        stats = {"candidates": len(rows), "pushed": 0, "errors": 0}
        service = GitHubSyncService(
            mcp_manager=self.mcp_manager,
            task_manager=self.task_manager,
            project_id=project_id,
            github_repo=project.github_repo,
        )
        for row in rows:
            try:
                await service.sync_task_to_github(row["id"])
                stats["pushed"] += 1
            except Exception as exc:
                if is_github_rate_limit_error(exc):
                    raise
                stats["errors"] += 1
                logger.warning(
                    "Failed to push linked GitHub task %s to %s#%s (%s): %s",
                    row["id"],
                    row["github_repo"],
                    row["github_issue_number"],
                    type(exc).__name__,
                    exc,
                )
        return stats

    async def _fetch_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        owner, repo_name = parse_github_repo(repo)
        result = await self._call(
            "get_issue",
            {"owner": owner, "repo": repo_name, "issue_number": issue_number},
        )
        if not isinstance(result, dict):
            raise RuntimeError("GitHub get_issue returned an invalid payload")
        return result

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        from gobby.mcp_proxy.services.server_resolution import as_project_id, resolved_server_id

        project_id = as_project_id(getattr(self.mcp_manager, "project_id", None))
        server_id = resolved_server_id(self.mcp_manager, "github", project_id=str(project_id))
        if server_id is None:
            raise RuntimeError(f"GitHub MCP server not found in project {project_id}")
        result = await self.mcp_manager.call_tool(
            server_id, tool_name=tool_name, arguments=arguments
        )
        return parse_github_mcp_result(result, tool_name)

    @staticmethod
    def _issue_updates(issue: dict[str, Any]) -> dict[str, Any]:
        labels = [
            label.get("name", "") if isinstance(label, dict) else str(label)
            for label in issue.get("labels") or []
        ]
        updates: dict[str, Any] = {
            "title": issue.get("title") or "Untitled Issue",
            "description": issue.get("body") or "",
            "labels": [label for label in labels if label],
        }
        if issue.get("state") == "closed":
            updates.update(
                closed_at=issue.get("closed_at") or datetime.now(UTC).isoformat(),
                closed_reason="github_sync",
                closed_in_session_id=None,
                closed_commit_sha=None,
            )
        elif issue.get("state") == "open":
            updates.update(
                closed_at=None,
                closed_reason=None,
                closed_in_session_id=None,
                closed_commit_sha=None,
            )
        return updates

    @staticmethod
    def _empty_stats() -> dict[str, int]:
        return {"scanned": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0}


class GitHubIssueDeliveryHandler:
    """Webhook callback that gates automated triage independently from issue sync."""

    def __init__(
        self,
        triage_service: Any,
    ) -> None:
        self.sync_service = GitHubIssueSyncService(
            db=triage_service.db,
            mcp_manager=triage_service.mcp_manager,
            task_manager=triage_service.task_manager,
            project_manager=triage_service.project_manager,
        )
        self.config_store = triage_service.store
        self.triage_issue: TriageIssueCallback = triage_service.triage_issue

    async def __call__(
        self,
        project_id: str,
        repo: str,
        issue_number: int,
        source: str,
        *,
        issue_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self.sync_service.sync_issue(
            project_id, repo, issue_number, issue_data=issue_data
        )
        if result.get("action") == "skipped_pull_request":
            return result
        config = await asyncio.to_thread(self.config_store.get_config, project_id)
        if config.triage_enabled:
            return await self.triage_issue(
                project_id,
                repo,
                issue_number,
                source=source,
                issue_data=issue_data,
            )
        return result


def _copy_retry_metadata(source: Exception, target: Exception) -> None:
    for name in ("retry_after_seconds", "retry_after"):
        value = getattr(source, name, None)
        if value is not None:
            setattr(target, name, value)
