"""Task import, push, pull, and bidirectional Linear sync operations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from gobby.integrations.linear_graphql import LinearGraphQLClient, LinearGraphQLError
from gobby.sync.linear_project_ops import LinearProjectOpsMixin
from gobby.sync.linear_support import (
    LinearSyncError,
    _extract_records,
    _gobby_seq_from_linear_title,
    _linear_fetch_failure_limiter,
    _local_title_from_linear,
    _parse_linear_mcp_result,
    is_transient_linear_fetch_error,
    logger,
    project_gobby_state_for_linear,
)
from gobby.sync.linear_support import (
    map_gobby_state_to_linear as _map_gobby_state_to_linear,
)
from gobby.sync.linear_support import (
    map_linear_state_to_gobby as _map_linear_state_to_gobby,
)
from gobby.tasks.import_criteria import external_issue_validation_criteria

_LINEAR_ISSUE_PAGE_SIZE = 100

_GOBBY_TO_LINEAR_PRIORITY = {
    0: 1,  # critical -> urgent
    1: 2,  # high -> high
    2: 3,  # medium -> medium
    3: 4,  # low -> low
    4: 0,  # backlog -> no priority
}
_LINEAR_TO_GOBBY_PRIORITY = {
    0: 4,  # no priority -> backlog
    1: 0,  # urgent -> critical
    2: 1,  # high -> high
    3: 2,  # medium -> medium
    4: 3,  # low -> low
}


def _gobby_priority_to_linear(priority: int) -> int:
    """Translate a Gobby priority, defaulting unsupported values to no priority."""
    return _GOBBY_TO_LINEAR_PRIORITY.get(priority, 0)


def _linear_priority_to_gobby(priority: Any) -> int:
    """Translate a Linear priority, defaulting missing or unsupported values to backlog."""
    if type(priority) is not int:
        return 4
    return _LINEAR_TO_GOBBY_PRIORITY.get(priority, 4)


def _next_linear_issue_cursor(result: Any, page_size: int) -> str | None:
    if not isinstance(result, dict):
        if page_size < _LINEAR_ISSUE_PAGE_SIZE:
            return None
        raise LinearSyncError("Linear MCP returned a full issue page without pagination metadata")

    pagination = result
    issues = result.get("issues")
    if isinstance(issues, dict):
        pagination = issues
    page_info = pagination.get("pageInfo")
    if isinstance(page_info, dict):
        pagination = page_info

    has_next_page = pagination.get("hasNextPage")
    if has_next_page is None:
        if page_size < _LINEAR_ISSUE_PAGE_SIZE:
            return None
        raise LinearSyncError("Linear MCP returned a full issue page without pagination metadata")
    if not isinstance(has_next_page, bool):
        raise LinearSyncError("Linear MCP returned invalid hasNextPage pagination metadata")
    if not has_next_page:
        return None

    cursor = pagination.get("cursor", pagination.get("endCursor"))
    if not isinstance(cursor, str) or not cursor:
        raise LinearSyncError("Linear MCP reported another issue page without a cursor")
    return cursor


def _parse_linear_timestamp(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_TIMESTAMP_FIELDS = {"closed_at", "escalated_at"}


def _linear_update_changes_task(row: Mapping[str, Any], updates: Mapping[str, Any]) -> bool:
    for field, value in updates.items():
        current = row.get(field)
        if field in _TIMESTAMP_FIELDS:
            if _parse_linear_timestamp(current) != _parse_linear_timestamp(value):
                return True
        elif field == "description":
            if (current or "") != (value or ""):
                return True
        elif current != value:
            return True
    return False


def _linear_issue_state_name(issue: dict[str, Any]) -> str | None:
    state = issue.get("state")
    if isinstance(state, dict):
        name = state.get("name")
        return name if isinstance(name, str) and name else None
    if isinstance(state, str) and state:
        return state

    status = issue.get("status")
    return status if isinstance(status, str) and status else None


def _linear_lifecycle_fields(
    gobby_state: str | None,
    *,
    state_name: str | None,
    changed_at: str,
) -> dict[str, Any]:
    if gobby_state is None:
        return {}
    if gobby_state == "closed":
        return {
            "closed_at": changed_at,
            "closed_reason": "linear_sync",
            "closed_in_session_id": None,
            "closed_commit_sha": None,
            "escalated_at": None,
            "escalation_reason": None,
        }
    if gobby_state == "escalated":
        return {
            "closed_at": None,
            "closed_reason": None,
            "closed_in_session_id": None,
            "closed_commit_sha": None,
            "escalated_at": changed_at,
            "escalation_reason": f"Linear state: {state_name or 'Canceled'}",
        }
    return {
        "closed_at": None,
        "closed_reason": None,
        "closed_in_session_id": None,
        "closed_commit_sha": None,
    }


class LinearTaskOpsMixin(LinearProjectOpsMixin):
    """Task-level Linear import, push, pull, and sync operations."""

    async def _list_issues_via_mcp(
        self,
        team_id: str,
        *,
        state: str | None = None,
        labels: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            result = await self.mcp_manager.call_tool(
                self._linear_server_id(),
                tool_name="list_issues",
                arguments=self._issue_list_args(
                    team_id,
                    state=state,
                    labels=labels,
                    cursor=cursor,
                ),
            )
            page = _extract_records(result)
            issues.extend(page)
            cursor = _next_linear_issue_cursor(result, len(page))
            if cursor is None:
                return issues
            if cursor in seen_cursors:
                raise LinearSyncError("Linear MCP repeated an issue pagination cursor")
            seen_cursors.add(cursor)

    async def import_linear_issues(
        self,
        team_id: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
        *,
        allow_team_wide: bool = False,
    ) -> list[dict[str, Any]]:
        """Import Linear issues as gobby tasks with dedup."""
        self.linear.require_available()

        effective_team_id = team_id or self.linear_team_id
        if not effective_team_id:
            raise ValueError("No team_id provided and no default linear_team_id configured.")
        if not self._get_linear_project_id() and not allow_team_wide:
            raise ValueError(
                "No Linear project binding configured. Run 'gobby linear setup --bootstrap' "
                "or explicitly allow a team-wide import."
            )

        try:
            if not self._linear_mcp_has_tool("list_issues"):
                raise LinearSyncError("Linear MCP server does not expose list_issues.")
            issues = await self._list_issues_via_mcp(
                effective_team_id,
                state=state,
                labels=labels,
            )
        except LinearSyncError:
            client = await self._get_graphql_client()
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

            title = issue.get("title", "Untitled Issue")
            local_title = _local_title_from_linear(title)
            description = issue.get("description", "")
            validation_criteria = external_issue_validation_criteria(
                "Linear",
                str(issue.get("identifier") or issue_id),
            )
            priority_val = _linear_priority_to_gobby(issue.get("priority"))
            state_name = _linear_issue_state_name(issue)
            gobby_state = (
                self.map_linear_state_to_gobby(state_name) if state_name is not None else None
            )
            linear_updated = _parse_linear_timestamp(issue.get("updatedAt"))
            changed_at = (
                linear_updated.isoformat()
                if linear_updated is not None
                else datetime.now(UTC).isoformat()
            )
            lifecycle_updates = _linear_lifecycle_fields(
                gobby_state,
                state_name=state_name,
                changed_at=changed_at,
            )

            with self.task_manager.db.transaction():
                existing = self.task_manager.db.fetchone(
                    "SELECT id, validation_criteria, updated_at FROM tasks "
                    "WHERE linear_issue_id = %s AND project_id = %s",
                    (issue_id, self.project_id),
                )
                needs_linear_link = False

                if not existing:
                    ref_seq = _gobby_seq_from_linear_title(title)
                    if ref_seq is not None:
                        existing = self.task_manager.db.fetchone(
                            "SELECT id, validation_criteria, updated_at FROM tasks "
                            "WHERE project_id = %s AND seq_num = %s",
                            (self.project_id, ref_seq),
                        )
                        if existing:
                            needs_linear_link = True

                if existing:
                    metadata_updates: dict[str, Any] = {}
                    if needs_linear_link:
                        metadata_updates.update(
                            linear_issue_id=issue_id,
                            linear_team_id=effective_team_id,
                        )
                    if existing.get("validation_criteria") != validation_criteria:
                        metadata_updates["validation_criteria"] = validation_criteria
                    if metadata_updates:
                        self.task_manager.update_task(
                            existing["id"],
                            **metadata_updates,
                        )
                    local_updated = _parse_linear_timestamp(existing.get("updated_at"))
                    is_stale = bool(
                        local_updated and linear_updated and local_updated > linear_updated
                    )
                    if not is_stale:
                        self.task_manager.reconcile_task_state(
                            existing["id"],
                            title=local_title,
                            description=description,
                            priority=priority_val,
                            **lifecycle_updates,
                        )
                    task = self.task_manager.get_task(existing["id"])
                    result_tasks.append(task.to_dict())
                else:
                    task = self.task_manager.create_task(
                        project_id=self.project_id,
                        title=local_title,
                        description=description,
                        linear_issue_id=issue_id,
                        linear_team_id=effective_team_id,
                        priority=priority_val,
                        validation_criteria=validation_criteria,
                    )
                    if any(value is not None for value in lifecycle_updates.values()):
                        task = self.task_manager.reconcile_task_state(
                            task.id,
                            **lifecycle_updates,
                        )
                    result_tasks.append(task.to_dict())

        logger.info("Imported %d issues from Linear team %s", len(result_tasks), effective_team_id)
        return result_tasks

    async def sync_task_to_linear(self, task_id: str) -> dict[str, Any]:
        """Sync a gobby task to its linked Linear issue."""
        self.linear.require_available()

        client = await self._get_graphql_client()
        return await self._sync_task_to_linear(
            task_id,
            client=client,
            state_ids_by_team={},
        )

    async def _sync_task_to_linear(
        self,
        task_id: str,
        *,
        client: LinearGraphQLClient | None,
        state_ids_by_team: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        task = await asyncio.to_thread(self.task_manager.get_task, task_id)
        if not task.linear_issue_id:
            raise ValueError(
                f"Task {task_id} has no linked Linear issue. Set linear_issue_id to sync."
            )

        linear_state = self.map_gobby_state_to_linear(self._project_gobby_state_for_linear(task))
        issue_title = self._linear_issue_title(task)
        if client:
            effective_team_id = task.linear_team_id or self.linear_team_id
            state_id = await self._linear_state_id_for_name(
                client,
                effective_team_id,
                linear_state,
                state_ids_by_team,
            )
            result = await client.update_issue(
                issue_id=task.linear_issue_id,
                title=issue_title,
                description=task.description or "",
                priority=_gobby_priority_to_linear(task.priority),
                state_id=state_id,
            )
            logger.info("Synced task %s to Linear issue %s", task_id, task.linear_issue_id)
            return result

        update_args: dict[str, Any] = {
            "id": task.linear_issue_id,
            "issueId": task.linear_issue_id,
            "title": issue_title,
            "description": task.description or "",
            "priority": _gobby_priority_to_linear(task.priority),
        }
        if linear_state:
            update_args["status"] = linear_state

        result = await self.mcp_manager.call_tool(
            self._linear_server_id(),
            tool_name="update_issue",
            arguments=update_args,
        )

        if result is None or not isinstance(result, dict):
            raise LinearSyncError(
                f"Invalid response from Linear MCP when updating issue "
                f"{task.linear_issue_id}: expected dict, got {type(result).__name__}"
            )

        logger.info("Synced task %s to Linear issue %s", task_id, task.linear_issue_id)
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

        existing_issue = await self._find_existing_issue_for_task(
            task=task,
            team_id=effective_team_id,
            project_id=linear_project_id,
            title=title,
        )
        if existing_issue:
            issue_id = existing_issue["id"]
            self.task_manager.update_task(
                task_id,
                linear_issue_id=issue_id,
                linear_team_id=effective_team_id,
            )
            logger.info("Registered %s in existing Linear issue %s", self._task_ref(task), issue_id)
            return self._decorate_issue_result(
                existing_issue,
                task,
                team_id=effective_team_id,
                project_id=linear_project_id,
            )

        client = await self._get_graphql_client()
        if client:
            result = await client.create_issue(
                team_id=effective_team_id,
                title=title,
                description=task.description or "",
                priority=_gobby_priority_to_linear(task.priority),
                project_id=linear_project_id,
            )
            result_dict = self._extract_created_issue(result, task_id)
            issue_id = result_dict.get("id")
            if issue_id:
                self.task_manager.update_task(
                    task_id,
                    linear_issue_id=issue_id,
                    linear_team_id=effective_team_id,
                )
                logger.info("Registered %s in Linear issue %s", self._task_ref(task), issue_id)
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
            "priority": _gobby_priority_to_linear(task.priority),
        }
        if linear_project_id:
            arguments["projectId"] = linear_project_id

        result = await self.mcp_manager.call_tool(
            self._linear_server_id(),
            tool_name="create_issue",
            arguments=arguments,
        )

        result_dict = self._extract_created_issue(result, task_id)
        issue_id = result_dict.get("id")
        if issue_id:
            self.task_manager.update_task(
                task_id,
                linear_issue_id=issue_id,
                linear_team_id=effective_team_id,
            )
            logger.info("Registered %s in Linear issue %s", self._task_ref(task), issue_id)

        return self._decorate_issue_result(
            result_dict,
            task,
            team_id=effective_team_id,
            project_id=linear_project_id,
        )

    async def _find_existing_issue_for_task(
        self,
        *,
        task: Any,
        team_id: str,
        project_id: str | None,
        title: str,
    ) -> dict[str, Any] | None:
        """Find a deterministic existing Linear issue for a Gobby task."""
        issues: list[dict[str, Any]] | None = None
        if self._linear_mcp_has_tool("list_issues"):
            try:
                issues = await self._list_issues_via_mcp(team_id)
            except LinearSyncError as exc:
                logger.warning(
                    "Failed to parse Linear issues while checking for existing %s: %s",
                    self._task_ref(task),
                    exc,
                )
        if issues is None:
            client = await self._get_graphql_client()
            if client:
                issues = await client.list_issues(team_id=team_id, project_id=project_id)
        if not issues:
            return None

        task_seq = getattr(task, "seq_num", None)
        for issue in issues:
            issue_id = issue.get("id")
            issue_title = issue.get("title")
            if not isinstance(issue_id, str) or not isinstance(issue_title, str):
                continue
            if issue_title == title:
                return issue
            if task_seq is not None and _gobby_seq_from_linear_title(issue_title) == task_seq:
                return issue
        return None

    async def create_missing_issues(
        self,
        team_id: str | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Create Linear issues for active non-closed Gobby tasks not linked yet."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero")
        effective_team_id = team_id or self.linear_team_id
        if not effective_team_id:
            raise ValueError("No team_id provided and no default linear_team_id configured.")

        sql = (
            "SELECT id FROM tasks "
            "WHERE project_id = %s AND linear_issue_id IS NULL AND closed_at IS NULL "
            "ORDER BY seq_num NULLS LAST, created_at, id"
        )
        params: tuple[Any, ...] = (self.project_id,)
        if limit is not None:
            sql += " LIMIT %s"
            params += (limit,)
        rows = await asyncio.to_thread(self.task_manager.db.fetchall, sql, params)

        created: list[dict[str, Any]] = []
        for row in rows:
            created.append(await self.create_issue_for_task(row["id"], team_id=effective_team_id))
        return created

    async def _push_task_rows(self, rows: list[Any]) -> dict[str, int]:
        stats = {"pushed": 0, "skipped": 0, "errors": 0, "deferred": 0}
        if not rows:
            return stats
        client = await self._get_graphql_client()
        state_ids_by_team: dict[str, dict[str, str]] = {}
        for row in rows:
            try:
                await self._sync_task_to_linear(
                    row["id"],
                    client=client,
                    state_ids_by_team=state_ids_by_team,
                )
                stats["pushed"] += 1
            except Exception as e:
                logger.warning("Failed to push task %s to Linear: %s", row["id"], e)
                stats["errors"] += 1
        return stats

    async def push_active_tasks(self) -> dict[str, int]:
        """Push all linked active non-closed Gobby tasks to Linear."""
        self.linear.require_available()
        rows = await asyncio.to_thread(
            self.task_manager.db.fetchall,
            "SELECT id FROM tasks "
            "WHERE project_id = %s AND linear_issue_id IS NOT NULL AND closed_at IS NULL",
            (self.project_id,),
        )
        return await self._push_task_rows(rows)

    async def sync_active_forward(self, team_id: str | None = None) -> dict[str, Any]:
        """Forward-only initial sync from active Gobby tasks into Linear."""
        effective_team_id = team_id or self.linear_team_id
        if not effective_team_id:
            raise ValueError("No team_id provided and no default linear_team_id configured.")

        created_issues = await self.create_missing_issues(team_id=effective_team_id)
        push_stats = await self.push_active_tasks()

        synced_at = None
        if push_stats.get("errors", 0) == 0 and push_stats.get("deferred", 0) == 0:
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
        """Pull updates from Linear for all linked tasks."""
        self.linear.require_available()

        effective_team_id = team_id or self.linear_team_id
        if not effective_team_id:
            raise ValueError("No team_id provided and no default linear_team_id configured.")

        synced_at = _parse_linear_timestamp(self._get_project_synced_at())
        stats = {"updated": 0, "skipped": 0, "errors": 0, "deferred": 0}

        rows = self.task_manager.db.fetchall(
            "SELECT id, linear_issue_id, title, description, priority, updated_at, "
            "closed_at, closed_reason, closed_in_session_id, closed_commit_sha, "
            "escalated_at, escalation_reason FROM tasks "
            "WHERE project_id = %s AND linear_issue_id IS NOT NULL",
            (self.project_id,),
        )
        if not rows:
            return stats

        try:
            if not self._linear_mcp_has_tool("list_issues"):
                raise LinearSyncError("Linear MCP server does not expose list_issues.")
            issues = await self._list_issues_via_mcp(effective_team_id)
        except LinearSyncError as e:
            client = await self._get_graphql_client()
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
                if is_transient_linear_fetch_error(graphql_error):
                    _linear_fetch_failure_limiter.log_deferred(logger, graphql_error)
                    stats["deferred"] = len(rows)
                else:
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
                logger.warning("Linked Linear issue %s was missing for task %s", linear_id, task_id)
                stats["errors"] += 1
                continue

            try:
                linear_updated = _parse_linear_timestamp(issue.get("updatedAt"))
                if synced_at and linear_updated and linear_updated <= synced_at:
                    stats["skipped"] += 1
                    continue

                state_name = _linear_issue_state_name(issue)
                gobby_state = (
                    self.map_linear_state_to_gobby(state_name) if state_name is not None else None
                )
                changed_at = (
                    linear_updated.isoformat()
                    if linear_updated is not None
                    else datetime.now(UTC).isoformat()
                )
                priority_val = _linear_priority_to_gobby(issue.get("priority"))
                updates = {
                    "title": _local_title_from_linear(issue.get("title", "")),
                    "description": issue.get("description", ""),
                    "priority": priority_val,
                    **_linear_lifecycle_fields(
                        gobby_state,
                        state_name=state_name,
                        changed_at=changed_at,
                    ),
                }
                if not _linear_update_changes_task(row, updates):
                    stats["skipped"] += 1
                    continue

                local_updated = _parse_linear_timestamp(row.get("updated_at"))
                if local_updated and linear_updated and local_updated > linear_updated:
                    logger.warning(
                        "Skipped Linear update for task %s because local changes are newer",
                        task_id,
                    )
                    stats["skipped"] += 1
                    continue

                self.task_manager.reconcile_task_state(task_id, **updates)
                stats["updated"] += 1
            except Exception as e:
                logger.warning("Failed to update task %s from Linear: %s", task_id, e)
                stats["errors"] += 1

        return stats

    async def push_dirty_tasks(self) -> dict[str, int]:
        """Push gobby tasks that changed since last sync to Linear."""
        self.linear.require_available()

        synced_at = await asyncio.to_thread(self._get_project_synced_at)
        if synced_at:
            rows = await asyncio.to_thread(
                self.task_manager.db.fetchall,
                "SELECT id FROM tasks "
                "WHERE project_id = %s AND linear_issue_id IS NOT NULL "
                "AND updated_at > %s",
                (self.project_id, synced_at),
            )
        else:
            rows = await asyncio.to_thread(
                self.task_manager.db.fetchall,
                "SELECT id FROM tasks WHERE project_id = %s AND linear_issue_id IS NOT NULL",
                (self.project_id,),
            )

        return await self._push_task_rows(rows)

    async def sync_all(self, team_id: str | None = None) -> dict[str, Any]:
        """Full bidirectional sync: pull first, then push."""
        effective_team_id = team_id or self.linear_team_id
        sync_started_at = datetime.now(UTC).isoformat()

        pull_stats = await self.pull_linear_updates(team_id=effective_team_id)
        pull_errors = int(pull_stats.get("errors", 0))
        pull_deferred = int(pull_stats.get("deferred", 0))
        if pull_errors or pull_deferred:
            push_stats = {"pushed": 0, "skipped": 0, "errors": 0, "deferred": 0}
        else:
            push_stats = await self.push_dirty_tasks()

        push_errors = int(push_stats.get("errors", 0))
        push_deferred = int(push_stats.get("deferred", 0))
        cursor_updated = not (pull_errors or pull_deferred or push_errors or push_deferred)
        synced_at: str | None
        if cursor_updated:
            synced_at = sync_started_at
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
        """Map gobby task state to Linear issue state name."""
        return _map_gobby_state_to_linear(gobby_state)

    def _extract_created_issue(self, result: Any, task_id: str) -> dict[str, Any]:
        result = _parse_linear_mcp_result(result)
        if isinstance(result, dict):
            issue = result.get("issue")
            if isinstance(issue, dict):
                result_dict = cast(dict[str, Any], issue)
            elif issue is not None:
                raise LinearSyncError(
                    f"Invalid response from Linear MCP when creating issue for task "
                    f"{task_id}: issue must be an object, got {type(issue).__name__}"
                )
            else:
                result_dict = cast(dict[str, Any], result)
            issue_id = result_dict.get("id")
            if not isinstance(issue_id, str) or not issue_id:
                raise LinearSyncError(
                    f"Invalid response from Linear MCP when creating issue for task "
                    f"{task_id}: missing required id"
                )
            return result_dict
        raise LinearSyncError(
            f"Invalid response from Linear MCP when creating issue for task "
            f"{task_id}: expected dict, got {type(result).__name__}"
        )

    def _project_gobby_state_for_linear(self, task: Any) -> str:
        return project_gobby_state_for_linear(task)

    def map_linear_state_to_gobby(self, linear_state: str) -> str:
        """Map Linear issue state to gobby task state."""
        return _map_linear_state_to_gobby(linear_state)
