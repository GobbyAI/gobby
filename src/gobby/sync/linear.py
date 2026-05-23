"""Linear sync service that orchestrates between gobby tasks and Linear.

This service delegates all Linear operations to the official Linear MCP server,
avoiding custom API client code. Supports bidirectional sync with state, priority,
dedup, and cron-based polling.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from gobby.integrations.linear import LinearIntegration
from gobby.integrations.linear_graphql import LinearGraphQLClient, LinearGraphQLError
from gobby.tasks.state_semantics import current_stage_state, is_task_closed, is_task_escalated
from gobby.utils.project_init import update_project_json_fields

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

_LINEAR_GOBBY_REF_TITLE_RE = re.compile(r"^#(?P<seq>\d+):\s*(?P<title>.+)$")
_LINEAR_FETCH_FAILURE_SUMMARY_INTERVAL = 10


class _RepeatedFetchFailureLimiter:
    """Suppress repeated identical fetch failures while preserving recovery visibility."""

    def __init__(self, *, summary_interval: int) -> None:
        self._summary_interval = summary_interval
        self._message: str | None = None
        self._suppressed_count = 0

    def reset(self) -> None:
        self._message = None
        self._suppressed_count = 0

    def log_failure(self, log: logging.Logger, error: BaseException) -> None:
        message = str(error)
        if message != self._message:
            self._log_changed_failure(log)
            self._message = message
            self._suppressed_count = 0
            log.error("Failed to fetch Linear issues: %s", message)
            return

        self._suppressed_count += 1
        if self._suppressed_count % self._summary_interval == 0:
            log.info(
                "Still failing to fetch Linear issues after %d suppressed repeat(s): %s",
                self._suppressed_count,
                message,
            )
            return

        log.debug(
            "Suppressing repeated Linear issue fetch failure #%d: %s",
            self._suppressed_count,
            message,
        )

    def log_success(self, log: logging.Logger) -> None:
        if self._message is None:
            return
        if self._suppressed_count:
            log.info(
                "Linear issue fetch recovered after %d suppressed repeat(s); last error: %s",
                self._suppressed_count,
                self._message,
            )
        else:
            log.info("Linear issue fetch recovered after previous failure: %s", self._message)
        self.reset()

    def _log_changed_failure(self, log: logging.Logger) -> None:
        if self._message is None or not self._suppressed_count:
            return
        log.info(
            "Linear issue fetch failure changed after %d suppressed repeat(s); previous error: %s",
            self._suppressed_count,
            self._message,
        )


_linear_fetch_failure_limiter = _RepeatedFetchFailureLimiter(
    summary_interval=_LINEAR_FETCH_FAILURE_SUMMARY_INTERVAL
)


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


def _extract_records(result: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []

    value = result.get(key) or result.get("nodes") or result.get("items")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _extract_record(result: Any, key: str) -> dict[str, Any]:
    if isinstance(result, dict):
        value = result.get(key)
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        return result
    return {}


def _gobby_seq_from_linear_title(title: str) -> int | None:
    match = _LINEAR_GOBBY_REF_TITLE_RE.match(title)
    if not match:
        return None
    return int(match.group("seq"))


def _local_title_from_linear(title: str) -> str:
    match = _LINEAR_GOBBY_REF_TITLE_RE.match(title)
    if not match:
        return title
    return match.group("title")


class LinearSyncService:
    """Service for syncing gobby tasks with Linear issues.

    This service orchestrates bidirectional sync between gobby tasks and Linear:
    - Import Linear issues as gobby tasks (with dedup)
    - Sync task updates back to Linear issues (state + priority)
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
        linear_project_id: str | None = None,
        project_manager: LocalProjectManager | None = None,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.task_manager = task_manager
        self.project_id = project_id
        self.linear_team_id = linear_team_id
        self.linear_project_id = linear_project_id
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

    def _get_linear_project_id(self) -> str | None:
        """Get the Linear project binding, preferring the service override."""
        if self.linear_project_id:
            return self.linear_project_id
        project = self.project_manager.get(self.project_id)
        return project.linear_project_id if project else None

    def _get_graphql_client(self) -> LinearGraphQLClient | None:
        return LinearGraphQLClient.from_database(self.task_manager.db)

    def _linear_mcp_has_tool(self, tool_name: str) -> bool:
        """Return False only when cached Linear tool metadata proves absence."""
        configs = getattr(self.mcp_manager, "server_configs", None)
        if not isinstance(configs, list):
            return True
        for config in configs:
            if getattr(config, "name", None) != "linear":
                continue
            tools = getattr(config, "tools", None)
            if tools is None:
                return True
            return any(tool.get("name") == tool_name for tool in tools if isinstance(tool, dict))
        return True

    def _linear_project_name(self) -> str:
        project = self.project_manager.get(self.project_id)
        if not project:
            raise LinearSyncError(f"Project not found: {self.project_id}")
        return Path(project.repo_path).name if project.repo_path else project.name

    def _linear_project_display_name(self) -> str | None:
        try:
            return self._linear_project_name()
        except Exception:
            return None

    def _task_ref(self, task: Any) -> str:
        seq_num = getattr(task, "seq_num", None)
        return f"#{seq_num}" if seq_num else str(getattr(task, "id", ""))[:8]

    def _linear_issue_title(self, task: Any) -> str:
        ref = self._task_ref(task)
        title = str(getattr(task, "title", "") or "")
        return title if title.startswith(ref) else f"{ref}: {title}"

    def _decorate_issue_result(
        self,
        result: dict[str, Any],
        task: Any,
        *,
        team_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        decorated = dict(result)
        decorated["gobby_ref"] = self._task_ref(task)
        decorated["gobby_task_id"] = task.id
        decorated["linear_team_id"] = team_id
        decorated["linear_project_id"] = project_id
        project_name = self._linear_project_display_name()
        if project_name:
            decorated["linear_project_name"] = project_name
        identifier = decorated.get("identifier")
        if isinstance(identifier, str):
            decorated["linear_identifier"] = identifier
        issue_id = decorated.get("id")
        if isinstance(issue_id, str):
            decorated["linear_issue_id"] = issue_id
        return decorated

    async def _linear_state_id_for_name(
        self,
        client: LinearGraphQLClient,
        team_id: str | None,
        state_name: str | None,
    ) -> str | None:
        if not team_id or not state_name:
            return None
        states = await client.list_team_states(team_id)
        for state in states:
            if state.get("name") == state_name:
                state_id = state.get("id")
                return state_id if isinstance(state_id, str) else None
        return None

    def _update_synced_at(self, timestamp: str | None = None) -> None:
        """Update the project's linear_synced_at cursor."""
        ts = timestamp or datetime.now(UTC).isoformat()
        self.project_manager.update(self.project_id, linear_synced_at=ts)

    async def list_teams(self) -> list[dict[str, Any]]:
        """List Linear teams available to the configured MCP auth."""
        self.linear.require_available()
        try:
            if not self._linear_mcp_has_tool("list_teams"):
                raise LinearSyncError("Linear MCP server does not expose list_teams.")
            result = await self.mcp_manager.call_tool(
                server_name="linear",
                tool_name="list_teams",
                arguments={},
            )
        except Exception as e:
            client = self._get_graphql_client()
            if client:
                return await client.list_teams()
            raise LinearSyncError(
                "Linear MCP server does not expose list_teams and no Linear API key "
                "is available for GraphQL discovery."
            ) from e
        return _extract_records(result, "teams")

    async def list_projects(self, team_id: str) -> list[dict[str, Any]]:
        """List Linear projects for a team."""
        self.linear.require_available()
        try:
            if not self._linear_mcp_has_tool("list_projects"):
                raise LinearSyncError("Linear MCP server does not expose list_projects.")
            result = await self.mcp_manager.call_tool(
                server_name="linear",
                tool_name="list_projects",
                arguments={"teamId": team_id},
            )
        except Exception as e:
            client = self._get_graphql_client()
            if client:
                return await client.list_projects(team_id)
            raise LinearSyncError(
                "Linear MCP server does not expose list_projects and no Linear API key "
                "is available for GraphQL project discovery."
            ) from e
        return _extract_records(result, "projects")

    async def ensure_linear_project(
        self,
        team_id: str,
        project_name: str,
        project_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Return an existing Linear project by name, or create it."""
        if project_id:
            return {"id": project_id, "name": project_name}, False

        for project in await self.list_projects(team_id):
            if project.get("name") == project_name:
                return project, False

        try:
            if not self._linear_mcp_has_tool("create_project"):
                raise LinearSyncError("Linear MCP server does not expose create_project.")
            result = await self.mcp_manager.call_tool(
                server_name="linear",
                tool_name="create_project",
                arguments={"teamId": team_id, "name": project_name},
            )
            project = _extract_record(result, "project")
        except Exception as e:
            client = self._get_graphql_client()
            if not client:
                raise LinearSyncError(
                    "Linear MCP server does not expose create_project and no Linear API key "
                    "is available for GraphQL project creation."
                ) from e
            project = await client.create_project(team_id, project_name)
        if not project.get("id"):
            raise LinearSyncError("Linear MCP create_project did not return a project id.")
        return project, True

    async def ensure_project_binding(self, team_id: str) -> str:
        """Ensure this Gobby project is bound to a same-named Linear project."""
        linear_project_id = self._get_linear_project_id()
        if linear_project_id:
            return linear_project_id

        project_name = self._linear_project_name()
        linear_project, _ = await self.ensure_linear_project(team_id, project_name)
        resolved_project_id = linear_project.get("id")
        if not isinstance(resolved_project_id, str) or not resolved_project_id:
            raise LinearSyncError("Linear project setup did not return a project id.")

        self.linear_project_id = resolved_project_id
        updated = self.project_manager.update(
            self.project_id,
            linear_team_id=team_id,
            linear_project_id=resolved_project_id,
        )
        if updated and updated.repo_path:
            update_project_json_fields(
                Path(updated.repo_path),
                linear_team_id=team_id,
                linear_project_id=resolved_project_id,
            )
        return resolved_project_id

    def _issue_list_args(
        self,
        team_id: str,
        state: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"teamId": team_id}
        linear_project_id = self._get_linear_project_id()
        if linear_project_id:
            args["projectId"] = linear_project_id
        if state:
            args["state"] = state
        if labels:
            args["labels"] = labels
        return args

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

        args = self._issue_list_args(effective_team_id, state=state, labels=labels)

        try:
            if not self._linear_mcp_has_tool("list_issues"):
                raise LinearSyncError("Linear MCP server does not expose list_issues.")
            result = await self.mcp_manager.call_tool(
                server_name="linear",
                tool_name="list_issues",
                arguments=args,
            )
            issues = result.get("issues", [])
        except Exception:
            client = self._get_graphql_client()
            if not client:
                raise
            issues = await client.list_issues(
                team_id=effective_team_id,
                project_id=self._get_linear_project_id(),
                state=state,
                labels=labels,
            )
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
            local_title = _local_title_from_linear(title)
            description = issue.get("description", "")
            priority_val = issue.get("priority", 2)

            if not existing:
                ref_seq = _gobby_seq_from_linear_title(title)
                if ref_seq is not None:
                    existing = self.task_manager.db.fetchone(
                        "SELECT id FROM tasks WHERE project_id = ? AND seq_num = ?",
                        (self.project_id, ref_seq),
                    )
                    if existing:
                        self.task_manager.update_task(
                            existing["id"],
                            linear_issue_id=issue_id,
                            linear_team_id=effective_team_id,
                        )

            if existing:
                # Update existing task
                self.task_manager.reconcile_task_state(
                    existing["id"],
                    title=local_title,
                    description=description,
                    priority=priority_val,
                )
                task = self.task_manager.get_task(existing["id"])
                result_tasks.append(task.to_dict())
            else:
                # Create new task
                task = self.task_manager.create_task(
                    project_id=self.project_id,
                    title=local_title,
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

        Updates the Linear issue title, description, state, and priority.

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

        linear_state = self.map_gobby_state_to_linear(self._project_gobby_state_for_linear(task))

        issue_title = self._linear_issue_title(task)
        client = self._get_graphql_client()
        if client:
            effective_team_id = task.linear_team_id or self.linear_team_id
            state_id = await self._linear_state_id_for_name(
                client,
                effective_team_id,
                linear_state,
            )
            result = await client.update_issue(
                issue_id=task.linear_issue_id,
                title=issue_title,
                description=task.description or "",
                priority=task.priority,
                state_id=state_id,
            )
            logger.info(f"Synced task {task_id} to Linear issue {task.linear_issue_id}")
            return result

        update_args: dict[str, Any] = {
            "id": task.linear_issue_id,
            "issueId": task.linear_issue_id,
            "title": issue_title,
            "description": task.description or "",
            "priority": task.priority,
        }
        # Only set state if we have a valid mapping
        if linear_state:
            update_args["status"] = linear_state

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

        linear_project_id = await self.ensure_project_binding(effective_team_id)
        title = self._linear_issue_title(task)

        client = self._get_graphql_client()
        if client:
            result_dict = await client.create_issue(
                team_id=effective_team_id,
                title=title,
                description=task.description or "",
                priority=task.priority,
                project_id=linear_project_id,
            )
            issue_id = result_dict.get("id")
            if issue_id:
                self.task_manager.update_task(
                    task_id,
                    linear_issue_id=issue_id,
                    linear_team_id=effective_team_id,
                )
                logger.info(f"Registered {self._task_ref(task)} in Linear issue {issue_id}")
            return self._decorate_issue_result(
                result_dict,
                task,
                team_id=effective_team_id,
                project_id=linear_project_id,
            )

        arguments: dict[str, Any] = {
            "teamId": effective_team_id,
            "title": title,
            "description": task.description or "",
            "priority": task.priority,
        }
        if linear_project_id:
            arguments["projectId"] = linear_project_id

        result = await self.mcp_manager.call_tool(
            server_name="linear",
            tool_name="create_issue",
            arguments=arguments,
        )

        result_dict = cast(dict[str, Any], result)
        issue_id = result_dict.get("id")
        if issue_id:
            self.task_manager.update_task(
                task_id,
                linear_issue_id=issue_id,
                linear_team_id=effective_team_id,
            )
            logger.info(f"Registered {self._task_ref(task)} in Linear issue {issue_id}")

        return self._decorate_issue_result(
            result_dict,
            task,
            team_id=effective_team_id,
            project_id=linear_project_id,
        )

    async def create_missing_issues(self, team_id: str | None = None) -> list[dict[str, Any]]:
        """Create Linear issues for active non-closed Gobby tasks not linked yet."""
        effective_team_id = team_id or self.linear_team_id
        if not effective_team_id:
            raise ValueError("No team_id provided and no default linear_team_id configured.")

        rows = self.task_manager.db.fetchall(
            "SELECT id FROM tasks "
            "WHERE project_id = ? AND linear_issue_id IS NULL AND closed_at IS NULL",
            (self.project_id,),
        )

        created: list[dict[str, Any]] = []
        for row in rows:
            created.append(await self.create_issue_for_task(row["id"], team_id=effective_team_id))
        return created

    async def _push_task_rows(self, rows: list[Any]) -> dict[str, int]:
        stats = {"pushed": 0, "skipped": 0, "errors": 0}
        for row in rows:
            try:
                await self.sync_task_to_linear(row["id"])
                stats["pushed"] += 1
            except Exception as e:
                logger.warning(f"Failed to push task {row['id']} to Linear: {e}")
                stats["errors"] += 1
        return stats

    async def push_active_tasks(self) -> dict[str, int]:
        """Push all linked active non-closed Gobby tasks to Linear."""
        self.linear.require_available()
        rows = self.task_manager.db.fetchall(
            "SELECT id FROM tasks "
            "WHERE project_id = ? AND linear_issue_id IS NOT NULL AND closed_at IS NULL",
            (self.project_id,),
        )
        return await self._push_task_rows(rows)

    async def sync_active_forward(self, team_id: str | None = None) -> dict[str, Any]:
        """Forward-only initial sync from active Gobby tasks into Linear.

        This deliberately avoids pull/import behavior and excludes closed local
        task history so first setup does not flood Linear with stale work.
        """
        effective_team_id = team_id or self.linear_team_id
        if not effective_team_id:
            raise ValueError("No team_id provided and no default linear_team_id configured.")

        created_issues = await self.create_missing_issues(team_id=effective_team_id)
        push_stats = await self.push_active_tasks()

        synced_at = datetime.now(UTC).isoformat()
        self._update_synced_at(synced_at)

        return {
            "mode": "forward_active",
            "created_count": len(created_issues),
            "created_issues": created_issues,
            "push": push_stats,
            "synced_at": synced_at,
        }

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
            if not self._linear_mcp_has_tool("list_issues"):
                raise LinearSyncError("Linear MCP server does not expose list_issues.")
            result = await self.mcp_manager.call_tool(
                server_name="linear",
                tool_name="list_issues",
                arguments=self._issue_list_args(effective_team_id),
            )
            issues = result.get("issues", [])
        except Exception as e:
            client = self._get_graphql_client()
            if not client:
                _linear_fetch_failure_limiter.log_failure(logger, e)
                stats["errors"] = len(rows)
                return stats
            try:
                issues = await client.list_issues(
                    team_id=effective_team_id,
                    project_id=self._get_linear_project_id(),
                )
            except (LinearGraphQLError, httpx.HTTPError) as graphql_error:
                _linear_fetch_failure_limiter.log_failure(logger, graphql_error)
                stats["errors"] = len(rows)
                return stats
        _linear_fetch_failure_limiter.log_success(logger)
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
                    title=_local_title_from_linear(issue.get("title", "")),
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

        return await self._push_task_rows(rows)

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

        pull_errors = int(pull_stats.get("errors", 0))
        push_errors = int(push_stats.get("errors", 0))
        cursor_updated = pull_errors == 0 and push_errors == 0
        synced_at: str | None
        if cursor_updated:
            synced_at = datetime.now(UTC).isoformat()
            self._update_synced_at(synced_at)
        else:
            synced_at = self._get_project_synced_at()

        return {
            "pull": pull_stats,
            "push": push_stats,
            "cursor_updated": cursor_updated,
            "synced_at": synced_at,
        }

    def map_gobby_state_to_linear(self, gobby_state: str) -> str:
        """Map gobby task state to Linear issue state name.

        Note: This returns the state *name*, not the state ID.
        The Linear MCP server resolves names to IDs internally.
        """
        state_map = {
            "ready": "Todo",
            "in_progress": "In Progress",
            "needs_review": "In Review",
            "review_approved": "Done",
            "closed": "Done",
            "escalated": "Canceled",
        }
        return state_map.get(gobby_state, "Todo")

    def _project_gobby_state_for_linear(self, task: Any) -> str:
        if is_task_closed(task):
            return "closed"
        if is_task_escalated(task):
            return "escalated"
        return current_stage_state(task) or "ready"

    def map_linear_state_to_gobby(self, linear_state: str) -> str:
        """Map Linear issue state to gobby task state."""
        state_map = {
            "Todo": "ready",
            "In Progress": "in_progress",
            "Done": "closed",
            "Canceled": "closed",
            "In Review": "in_progress",
            "Backlog": "ready",
            "Triage": "ready",
        }
        return state_map.get(linear_state, "ready")


def create_linear_sync_handler(
    mcp_manager: MCPClientManager,
    task_manager: LocalTaskManager,
    project_id: str,
    team_id: str,
    linear_project_id: str | None = None,
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
            linear_project_id=linear_project_id,
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
