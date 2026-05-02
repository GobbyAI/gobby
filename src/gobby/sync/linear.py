"""Linear sync service that orchestrates between gobby tasks and Linear.

This service delegates all Linear operations to the official Linear MCP server,
avoiding custom API client code. Supports bidirectional sync with status, priority,
dedup, and cron-based polling.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from gobby.integrations.linear import LinearIntegration
from gobby.tasks.state_semantics import current_stage_state, is_task_closed, is_task_escalated

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

__all__ = [
    "LinearSyncService",
    "LinearSyncError",
    "LinearRateLimitError",
    "LinearNotFoundError",
    "create_linear_sync_handler",
]

logger = logging.getLogger(__name__)


class LinearSyncError(Exception):
    """Base exception for Linear sync errors."""

    pass


class LinearRateLimitError(LinearSyncError):
    """Raised when Linear API rate limit is exceeded.

    Attributes:
        reset_at: Unix timestamp when rate limit resets.
    """

    def __init__(self, message: str, reset_at: int | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class LinearNotFoundError(LinearSyncError):
    """Raised when a Linear resource is not found.

    Attributes:
        resource: Type of resource (e.g., "issue", "team", "project").
        resource_id: Identifier of the missing resource.
    """

    def __init__(
        self,
        message: str,
        resource: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.resource = resource
        self.resource_id = resource_id


class LinearSyncService:
    """Service for syncing gobby tasks with Linear issues.

    This service orchestrates bidirectional sync between gobby tasks and Linear:
    - Import Linear issues as gobby tasks (with dedup)
    - Sync task updates back to Linear issues (status + priority)
    - Pull updates from Linear to gobby tasks
    - Push dirty gobby tasks to Linear
    - Full bidirectional sync with loop prevention via project cursor

    All Linear operations are delegated to the official Linear MCP server.
    """

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        task_manager: LocalTaskManager,
        project_id: str,
        linear_team_id: str | None = None,
        project_manager: LocalProjectManager | None = None,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.task_manager = task_manager
        self.project_id = project_id
        self.linear_team_id = linear_team_id
        self.linear = LinearIntegration(mcp_manager)
        self._project_manager = project_manager

    @property
    def project_manager(self) -> LocalProjectManager:
        """Lazy-init project manager from task_manager's db if not provided."""
        if self._project_manager is None:
            from gobby.storage.projects import LocalProjectManager

            self._project_manager = LocalProjectManager(self.task_manager.db)
        return self._project_manager

    def is_available(self) -> bool:
        """Check if Linear MCP server is available."""
        return self.linear.is_available()

    def _get_project_synced_at(self) -> str | None:
        """Get the project's linear_synced_at cursor."""
        project = self.project_manager.get(self.project_id)
        return project.linear_synced_at if project else None

    def _update_synced_at(self, timestamp: str | None = None) -> None:
        """Update the project's linear_synced_at cursor."""
        ts = timestamp or datetime.now(UTC).isoformat()
        self.project_manager.update(self.project_id, linear_synced_at=ts)

    async def import_linear_issues(
        self,
        team_id: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Import Linear issues as gobby tasks with dedup.

        If a task with the same linear_issue_id already exists, it is updated
        instead of duplicated.

        Args:
            team_id: Linear team ID to filter issues. Uses default if not provided.
            state: Issue state to filter (e.g., "In Progress", "Todo").
            labels: Optional list of labels to filter issues.

        Returns:
            List of created/updated task dictionaries.
        """
        self.linear.require_available()

        effective_team_id = team_id or self.linear_team_id
        if not effective_team_id:
            raise ValueError("No team_id provided and no default linear_team_id configured.")

        args: dict[str, Any] = {"teamId": effective_team_id}
        if state:
            args["state"] = state
        if labels:
            args["labels"] = labels

        result = await self.mcp_manager.call_tool(
            server_name="linear",
            tool_name="list_issues",
            arguments=args,
        )

        issues = result.get("issues", [])
        result_tasks: list[dict[str, Any]] = []

        for issue in issues:
            issue_id = issue.get("id")
            if not issue_id:
                continue

            # Dedup: check if task with this linear_issue_id already exists
            existing = self.task_manager.db.fetchone(
                "SELECT id FROM tasks WHERE linear_issue_id = ? AND project_id = ?",
                (issue_id, self.project_id),
            )

            title = issue.get("title", "Untitled Issue")
            description = issue.get("description", "")
            priority_val = issue.get("priority", 2)

            if existing:
                # Update existing task
                self.task_manager.reconcile_task_state(
                    existing["id"],
                    title=title,
                    description=description,
                    priority=priority_val,
                )
                task = self.task_manager.get_task(existing["id"])
                result_tasks.append(task.to_dict())
            else:
                # Create new task
                task = self.task_manager.create_task(
                    project_id=self.project_id,
                    title=title,
                    description=description,
                    linear_issue_id=issue_id,
                    linear_team_id=effective_team_id,
                    priority=priority_val,
                )
                result_tasks.append(task.to_dict())

        logger.info(f"Imported {len(result_tasks)} issues from Linear team {effective_team_id}")
        return result_tasks

    async def sync_task_to_linear(self, task_id: str) -> dict[str, Any]:
        """Sync a gobby task to its linked Linear issue.

        Updates the Linear issue title, description, status, and priority.

        Args:
            task_id: ID of the task to sync.

        Returns:
            Result from Linear MCP update_issue call.
        """
        self.linear.require_available()

        task = self.task_manager.get_task(task_id)

        if not task.linear_issue_id:
            raise ValueError(
                f"Task {task_id} has no linked Linear issue. Set linear_issue_id to sync."
            )

        linear_state = self.map_gobby_status_to_linear(self._project_gobby_state_for_linear(task))

        update_args: dict[str, Any] = {
            "issueId": task.linear_issue_id,
            "title": task.title,
            "description": task.description or "",
            "priority": task.priority,
        }
        # Only set state if we have a valid mapping
        if linear_state:
            update_args["stateId"] = linear_state

        result = await self.mcp_manager.call_tool(
            server_name="linear",
            tool_name="update_issue",
            arguments=update_args,
        )

        if result is None or not isinstance(result, dict):
            raise LinearSyncError(
                f"Invalid response from Linear MCP when updating issue "
                f"{task.linear_issue_id}: expected dict, got {type(result).__name__}"
            )

        logger.info(f"Synced task {task_id} to Linear issue {task.linear_issue_id}")
        return cast(dict[str, Any], result)

    async def create_issue_for_task(
        self,
        task_id: str,
        team_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Linear issue from a gobby task."""
        self.linear.require_available()

        task = self.task_manager.get_task(task_id)

        effective_team_id = team_id or task.linear_team_id or self.linear_team_id
        if not effective_team_id:
            raise ValueError(f"Task {task_id} has no linear_team_id set and no default configured.")

        result = await self.mcp_manager.call_tool(
            server_name="linear",
            tool_name="create_issue",
            arguments={
                "teamId": effective_team_id,
                "title": task.title,
                "description": task.description or "",
                "priority": task.priority,
            },
        )

        result_dict = cast(dict[str, Any], result)
        issue_id = result_dict.get("id")
        if issue_id:
            self.task_manager.update_task(
                task_id,
                linear_issue_id=issue_id,
                linear_team_id=effective_team_id,
            )
            logger.info(f"Created Linear issue {issue_id} for task {task_id}")

        return result_dict

    async def pull_linear_updates(self, team_id: str | None = None) -> dict[str, int]:
        """Pull updates from Linear for all linked tasks.

        Compares Linear's updatedAt against the project's linear_synced_at cursor.
        Only updates tasks where Linear is newer.

        Args:
            team_id: Linear team ID. Uses default if not provided.

        Returns:
            Dict with updated, skipped, errors counts.
        """
        self.linear.require_available()

        effective_team_id = team_id or self.linear_team_id
        if not effective_team_id:
            raise ValueError("No team_id provided and no default linear_team_id configured.")

        synced_at = self._get_project_synced_at()
        stats = {"updated": 0, "skipped": 0, "errors": 0}

        # Get all linked tasks for this project
        rows = self.task_manager.db.fetchall(
            "SELECT id, linear_issue_id FROM tasks "
            "WHERE project_id = ? AND linear_issue_id IS NOT NULL",
            (self.project_id,),
        )

        if not rows:
            return stats

        # Fetch issues from Linear
        try:
            result = await self.mcp_manager.call_tool(
                server_name="linear",
                tool_name="list_issues",
                arguments={"teamId": effective_team_id},
            )
        except Exception as e:
            logger.error(f"Failed to fetch Linear issues: {e}")
            stats["errors"] = len(rows)
            return stats

        issues = result.get("issues", [])
        issue_map = {issue.get("id"): issue for issue in issues if issue.get("id")}

        for row in rows:
            task_id = row["id"]
            linear_id = row["linear_issue_id"]
            issue = issue_map.get(linear_id)

            if not issue:
                stats["skipped"] += 1
                continue

            try:
                # Check if Linear issue was updated after our last sync
                linear_updated = issue.get("updatedAt", "")
                if synced_at and linear_updated and linear_updated <= synced_at:
                    stats["skipped"] += 1
                    continue

                # Update task from Linear data
                priority_val = issue.get("priority", 2)

                self.task_manager.reconcile_task_state(
                    task_id,
                    title=issue.get("title", ""),
                    description=issue.get("description", ""),
                    priority=priority_val,
                )
                stats["updated"] += 1
            except Exception as e:
                logger.warning(f"Failed to update task {task_id} from Linear: {e}")
                stats["errors"] += 1

        return stats

    async def push_dirty_tasks(self) -> dict[str, int]:
        """Push gobby tasks that changed since last sync to Linear.

        Finds tasks where updated_at > project.linear_synced_at and
        pushes them to their linked Linear issues.

        Returns:
            Dict with pushed, skipped, errors counts.
        """
        self.linear.require_available()

        synced_at = self._get_project_synced_at()
        stats = {"pushed": 0, "skipped": 0, "errors": 0}

        # Query tasks that are linked and modified since last sync
        if synced_at:
            rows = self.task_manager.db.fetchall(
                "SELECT id FROM tasks "
                "WHERE project_id = ? AND linear_issue_id IS NOT NULL "
                "AND updated_at > ?",
                (self.project_id, synced_at),
            )
        else:
            # No previous sync — push all linked tasks
            rows = self.task_manager.db.fetchall(
                "SELECT id FROM tasks WHERE project_id = ? AND linear_issue_id IS NOT NULL",
                (self.project_id,),
            )

        for row in rows:
            try:
                await self.sync_task_to_linear(row["id"])
                stats["pushed"] += 1
            except Exception as e:
                logger.warning(f"Failed to push task {row['id']} to Linear: {e}")
                stats["errors"] += 1

        return stats

    async def sync_all(self, team_id: str | None = None) -> dict[str, Any]:
        """Full bidirectional sync: pull first, then push.

        Order matters for loop prevention:
        1. Pull from Linear (updates tasks where Linear is newer)
        2. Push dirty tasks (tasks changed after last sync)
        3. Update project.linear_synced_at = now

        Args:
            team_id: Linear team ID. Uses default if not provided.

        Returns:
            Dict with pull and push results.
        """
        effective_team_id = team_id or self.linear_team_id

        pull_stats = await self.pull_linear_updates(team_id=effective_team_id)
        push_stats = await self.push_dirty_tasks()

        # Update cursor after both complete
        self._update_synced_at()

        return {
            "pull": pull_stats,
            "push": push_stats,
            "synced_at": datetime.now(UTC).isoformat(),
        }

    def map_gobby_status_to_linear(self, gobby_status: str) -> str:
        """Map gobby task status to Linear issue state name.

        Note: This returns the state *name*, not the state ID.
        The Linear MCP server resolves names to IDs internally.
        """
        status_map = {
            "open": "Todo",
            "in_progress": "In Progress",
            "needs_review": "In Review",
            "review_approved": "Done",
            "closed": "Done",
            "escalated": "Canceled",
        }
        return status_map.get(gobby_status, "Todo")

    def _project_gobby_state_for_linear(self, task: Any) -> str:
        if is_task_closed(task):
            return "closed"
        if is_task_escalated(task):
            return "escalated"
        return current_stage_state(task) or "open"

    def map_linear_status_to_gobby(self, linear_state: str) -> str:
        """Map Linear issue state to gobby task status."""
        state_map = {
            "Todo": "open",
            "In Progress": "in_progress",
            "Done": "closed",
            "Canceled": "closed",
            "In Review": "in_progress",
            "Backlog": "open",
            "Triage": "open",
        }
        return state_map.get(linear_state, "open")


def create_linear_sync_handler(
    mcp_manager: MCPClientManager,
    task_manager: LocalTaskManager,
    project_id: str,
    team_id: str,
) -> Any:
    """Create a cron handler for periodic Linear sync.

    Returns an async callable compatible with CronExecutor.register_handler().
    """
    from gobby.storage.cron_models import CronJob

    async def linear_sync_handler(job: CronJob) -> str:
        service = LinearSyncService(
            mcp_manager=mcp_manager,
            task_manager=task_manager,
            project_id=project_id,
            linear_team_id=team_id,
        )

        if not service.is_available():
            return "Linear MCP server unavailable, skipping sync"

        try:
            result = await service.sync_all(team_id=team_id)
            pull = result["pull"]
            push = result["push"]
            return (
                f"Linear sync complete: "
                f"pulled {pull['updated']} (skipped {pull['skipped']}, errors {pull['errors']}), "
                f"pushed {push['pushed']} (errors {push['errors']})"
            )
        except Exception as e:
            logger.error(f"Linear sync cron failed: {e}", exc_info=True)
            return f"Linear sync failed: {e}"

    return linear_sync_handler
