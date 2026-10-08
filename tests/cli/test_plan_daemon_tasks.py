"""Agent plan validation reads completed-section owners through the daemon."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from gobby.cli._plan_daemon_tasks import DaemonTaskLookup
from gobby.tasks.expansion._validate import CompletedSectionExemptionsUnavailable
from gobby.utils.daemon_client import DaemonAuthenticationError

pytestmark = pytest.mark.unit

LABEL = "covers:cli-plan:1.1:1.1.1"


def task_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": "task-1",
        "project_id": "project-1",
        "title": "Work",
        "priority": 2,
        "task_type": "task",
        "created_at": "2026-10-07T01:00:00+00:00",
        "updated_at": "2026-10-07T02:00:00+00:00",
        "closed_at": "2026-10-07T03:00:00+00:00",
        "labels": [LABEL],
        "closed_reason": "completed",
        "commits": ["abc1234"],
        "seq_num": 7,
    }
    return record | overrides


class FakeTaskClient:
    """Answers gobby-tasks calls the way the daemon's MCP route wraps them."""

    def __init__(
        self,
        tasks: list[dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
        envelope: dict[str, Any] | None = None,
    ) -> None:
        self.tasks = {task["id"]: task for task in tasks or []}
        self.error = error
        self.envelope = envelope
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call_mcp_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((server_name, tool_name, arguments))
        if self.error is not None:
            raise self.error
        if self.envelope is not None:
            return self.envelope
        if tool_name == "list_tasks":
            rows = [
                {"id": task["id"]}
                for task in self.tasks.values()
                if arguments["label"] in task["labels"]
                and task["project_id"] == arguments["project"]
            ][: arguments["limit"]]
            return {"success": True, "result": {"tasks": rows, "count": len(rows)}}
        return {"success": True, "result": self.tasks[arguments["task_id"]]}


def test_lookup_rebuilds_full_owner_records_and_fetches_each_once() -> None:
    client = FakeTaskClient([task_record()])
    lookup = DaemonTaskLookup(client)

    first = lookup.list_tasks(project_id="project-1", label=LABEL, limit=2, sort_by="updated_at")
    second = lookup.list_tasks(project_id="project-1", label=LABEL, limit=2, sort_by="updated_at")

    assert first == second
    [task] = first
    assert (task.id, task.project_id, task.labels) == ("task-1", "project-1", [LABEL])
    assert (task.closed_reason, task.commits, task.seq_num) == ("completed", ["abc1234"], 7)
    assert task.closed_at == datetime(2026, 10, 7, 3, tzinfo=UTC)
    assert client.calls == [
        ("gobby-tasks", "list_tasks", {"project": "project-1", "label": LABEL, "limit": 2}),
        ("gobby-tasks", "get_task", {"task_id": "task-1", "brief": False}),
        ("gobby-tasks", "list_tasks", {"project": "project-1", "label": LABEL, "limit": 2}),
    ]


def test_lookup_keeps_open_owners_open() -> None:
    client = FakeTaskClient([task_record(closed_at=None, closed_reason=None)])

    [task] = DaemonTaskLookup(client).list_tasks(
        project_id="project-1", label=LABEL, limit=2, sort_by="updated_at"
    )

    assert (task.closed_at, task.closed_reason) == (None, None)


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (
            FakeTaskClient(error=httpx.ConnectError("connection refused")),
            "daemon task API list_tasks failed: connection refused",
        ),
        (
            FakeTaskClient(error=DaemonAuthenticationError("Daemon authentication failed")),
            "daemon task API list_tasks failed: Daemon authentication failed",
        ),
        (
            FakeTaskClient(envelope={"success": False, "error": "tool denied"}),
            "daemon task API list_tasks failed: tool denied",
        ),
        (
            FakeTaskClient(
                envelope={"success": True, "result": {"error": "no project", "tasks": []}}
            ),
            "daemon task API list_tasks failed: no project",
        ),
        (
            FakeTaskClient(envelope={"success": True, "result": {"count": 0}}),
            "daemon task API list_tasks returned a malformed page: KeyError('tasks')",
        ),
        (
            FakeTaskClient([task_record(created_at="yesterday")]),
            "daemon task API get_task returned a malformed task task-1",
        ),
    ],
    ids=["transport", "auth", "tool-failure", "tool-error", "malformed-page", "malformed-task"],
)
def test_lookup_failures_raise_the_named_condition(client: FakeTaskClient, message: str) -> None:
    with pytest.raises(CompletedSectionExemptionsUnavailable) as raised:
        DaemonTaskLookup(client).list_tasks(
            project_id="project-1", label=LABEL, limit=2, sort_by="updated_at"
        )

    assert str(raised.value).startswith(message)
