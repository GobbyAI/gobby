"""Agent-principal task reads for plan validation, through the daemon.

Managed-execution grants carry no privileges on ``tasks``, so an agent's
``gobby plans validate`` resolves completed-section owners through the daemon's
gobby-tasks tools instead of the hub (the operator, #23620). This stands
until gobby-tasks is rebuilt in Rust.
"""

from __future__ import annotations

import json
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

    def list_tasks(
        self,
        *,
        project_id: str,
        limit: int,
        offset: int,
        sort_by: str,
        label: str | None = None,
        labels: list[str] | None = None,
    ) -> list[Task]:
        """Return a stable page of project tasks matching exact label filters."""
        page = self._call(
            "list_tasks",
            {
                "project": project_id,
                **({"label": label} if label is not None else {}),
                **({"labels": labels} if labels is not None else {}),
                "limit": limit,
                "offset": offset,
                "sort_by": sort_by,
            },
        )
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

    def _call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        server_name: str = "gobby-tasks",
    ) -> dict[str, Any]:
        try:
            response = self._client.call_mcp_tool(server_name, tool_name, arguments)
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
        if result.get("offloaded") is True:
            return self._offloaded_result(result)
        return result

    def _offloaded_result(self, projection: dict[str, Any]) -> dict[str, Any]:
        try:
            result_id = projection["result_id"]
            if not isinstance(result_id, str) or not result_id:
                raise ValueError("missing result_id")
            chunks: list[str] = []
            offset = 0
            while True:
                page = self._call(
                    "get_tool_result",
                    {"result_id": result_id, "offset": offset, "limit": 14000},
                    server_name="gobby-results",
                )
                content = page["content"]
                if (
                    not isinstance(content, str)
                    or page["offset"] != offset
                    or page["stored_chars"] != page["total_chars"]
                ):
                    raise ValueError("malformed or truncated result page")
                chunks.append(content)
                offset += len(content)
                next_offset = page["next_offset"]
                if next_offset is None:
                    if offset != page["stored_chars"]:
                        raise ValueError("incomplete result")
                    break
                if type(next_offset) is not int or next_offset != offset or not content:
                    raise ValueError("result pagination did not advance")
            record = json.loads("".join(chunks))
            if not isinstance(record, dict):
                raise ValueError("result is not an object")
            return record
        except (KeyError, TypeError, ValueError) as exc:
            raise CompletedSectionExemptionsUnavailable(
                f"daemon task API offloaded result is malformed: {exc!r}"
            ) from exc
