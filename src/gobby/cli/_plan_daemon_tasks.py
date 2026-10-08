"""Agent-principal task reads for plan validation, through the daemon.

Managed-execution grants carry no privileges on ``tasks``, so an agent's
``gobby plans validate`` resolves completed-section owners through the daemon's
gobby-tasks tools instead of the hub (Josh, 2026-10-07, #23620). This stands
until gobby-tasks is rebuilt in Rust.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

import httpx

from gobby.storage.tasks import Task
from gobby.tasks.expansion._validate import CompletedSectionExemptionsUnavailable
from gobby.utils.daemon_client import DaemonAuthenticationError


class McpToolCaller(Protocol):
    """The daemon client surface this lookup uses."""

    def call_mcp_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call one daemon MCP tool and return its response envelope."""
        ...


class DaemonTaskLookup:
    """Serve ``CompletionTaskLookup`` from the daemon's gobby-tasks tools."""

    def __init__(self, client: McpToolCaller) -> None:
        self._client = client
        self._tasks: dict[str, Task] = {}

    def list_tasks(self, *, project_id: str, label: str, limit: int, sort_by: str) -> list[Task]:
        """Return up to ``limit`` project tasks carrying ``label``."""
        # Ownership needs exactly one match, so the order of a short page never matters.
        del sort_by
        page = self._call("list_tasks", {"project": project_id, "label": label, "limit": limit})
        try:
            task_ids = [str(row["id"]) for row in page["tasks"]]
        except (KeyError, TypeError) as exc:
            raise CompletedSectionExemptionsUnavailable(
                f"daemon task API list_tasks returned a malformed page: {exc!r}"
            ) from exc
        return [self._task(task_id) for task_id in task_ids]

    def _task(self, task_id: str) -> Task:
        if task_id not in self._tasks:
            record = self._call("get_task", {"task_id": task_id, "brief": False})
            try:
                closed_at = record["closed_at"]
                self._tasks[task_id] = Task(
                    id=record["id"],
                    project_id=record["project_id"],
                    title=record["title"],
                    priority=record["priority"],
                    task_type=record["task_type"],
                    created_at=datetime.fromisoformat(record["created_at"]),
                    updated_at=datetime.fromisoformat(record["updated_at"]),
                    closed_at=datetime.fromisoformat(closed_at) if closed_at else None,
                    labels=record["labels"],
                    closed_reason=record["closed_reason"],
                    commits=record["commits"],
                    seq_num=record["seq_num"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CompletedSectionExemptionsUnavailable(
                    f"daemon task API get_task returned a malformed task {task_id}: {exc!r}"
                ) from exc
        return self._tasks[task_id]

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.call_mcp_tool("gobby-tasks", tool_name, arguments)
        except (httpx.HTTPError, DaemonAuthenticationError, ValueError) as exc:
            raise CompletedSectionExemptionsUnavailable(
                f"daemon task API {tool_name} failed: {exc}"
            ) from exc
        result = response.get("result")
        if response.get("success") is not True or not isinstance(result, dict):
            raise CompletedSectionExemptionsUnavailable(
                f"daemon task API {tool_name} failed: {response.get('error', response)}"
            )
        if "error" in result:
            raise CompletedSectionExemptionsUnavailable(
                f"daemon task API {tool_name} failed: {result['error']}"
            )
        return result
