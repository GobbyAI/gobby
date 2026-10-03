"""Run list payload: identity and decision fields plus the run's seat (deploy-runbook 6.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.agents_run_payload import _list_run_payload
from gobby.storage.agents import AgentRun

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 2, 19, 30, tzinfo=UTC)
_PLACEMENT = {
    "kind": "split",
    "workspace_id": "ws-runbook",
    "title": "L3",
    "beside_pane_id": "pane-7",
    "axis": "vertical",
}


def _run(resume_metadata: dict[str, Any] | None) -> AgentRun:
    return AgentRun(
        id="run-1",
        parent_session_id="sess-parent",
        machine_id="machine-1",
        provider="claude",
        prompt="build the runbook",
        status="running",
        created_at=_NOW,
        updated_at=_NOW,
        started_at=_NOW,
        agent_name="backend-developer",
        task_id="task-uuid",
        tool_calls_count=4,
        turns_used=2,
        resume_metadata_json=resume_metadata,
    )


@pytest.mark.parametrize(
    ("resume_metadata", "seat"),
    [
        pytest.param(
            {"placement": _PLACEMENT}, {"workspace": "ws-runbook", "title": "L3"}, id="placed"
        ),
        pytest.param(None, None, id="no-metadata"),
        pytest.param({"branch_name": "task-1"}, None, id="unplaced"),
        pytest.param(
            {"placement": {"kind": "tab", "workspace_id": "ws-runbook"}}, None, id="no-title"
        ),
        pytest.param({"placement": {"kind": "tab", "title": "L3"}}, None, id="no-workspace"),
        pytest.param({"placement": "ws-runbook"}, None, id="not-a-mapping"),
    ],
)
async def test_seat_from_placement_metadata(
    resume_metadata: dict[str, Any] | None,
    seat: dict[str, str] | None,
) -> None:
    run = _run(resume_metadata)
    runner = MagicMock()
    runner.list_runs.return_value = [run]
    runner.run_storage.list_active_global.return_value = [run]
    registry = create_agents_registry(runner)

    listed = registry._tools["list_agent_runs"].func(parent_session_id="sess-parent")
    running = await registry._tools["list_running_agents"].func()

    assert _list_run_payload(run)["seat"] == seat
    assert listed["runs"][0]["seat"] == seat
    assert running["agents"][0]["seat"] == seat


@pytest.mark.parametrize(
    ("resume_metadata", "task_ref", "branch_name"),
    [
        pytest.param(
            {"task_ref": "#23328", "branch_name": "task-23328-seat-field", "placement": _PLACEMENT},
            "#23328",
            "task-23328-seat-field",
            id="from-metadata",
        ),
        pytest.param(None, "task-uuid", None, id="fallbacks"),
    ],
)
def test_payload_fields_unchanged(
    resume_metadata: dict[str, Any] | None,
    task_ref: str,
    branch_name: str | None,
) -> None:
    run = _run(resume_metadata)

    payload = _list_run_payload(run)

    assert {key: value for key, value in payload.items() if key != "seat"} == {
        **run.liveness_payload(),
        "run_id": "run-1",
        "task_ref": task_ref,
        "agent_name": "backend-developer",
        "status": "running",
        "started_at": _NOW,
        "branch_name": branch_name,
        "tool_calls_count": 4,
        "turns_used": 2,
    }
