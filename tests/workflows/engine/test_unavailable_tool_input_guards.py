"""Enforcement and guard readers treat a marked-unavailable tool input as unknown (#23168)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.commit_guard import foreign_staged_commit_conflict
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.engine.enforcement_checks import EnforcementCheckMixin
from gobby.workflows.engine.event_utils import _target_task_tool_input
from gobby.workflows.step_instances import AgentStepInstance

pytestmark = pytest.mark.unit

SESSION_ID = "33333333-3333-4333-8333-333333333333"
TRUNCATED_CALL = '{"server_name": "gobby-tasks", "tool_name": "close_task", "arguments": {"task'
TRUNCATED_SHELL = '{"command": "git commit -am wip && git pu'


class _Harness(EnforcementCheckMixin):
    def __init__(self, step: WorkflowStep | None = None) -> None:
        self._step = step
        self._instance = MagicMock(spec=AgentStepInstance)
        self._instance.agent_name = "backend-developer"
        self._instance.variables = {}
        self.instance_manager = MagicMock()
        self.audited: list[str] = []

    def _get_step_for_session(
        self, session_id: str
    ) -> tuple[WorkflowStep | None, AgentStepInstance | None]:
        return (self._step, self._instance) if self._step is not None else (None, None)

    def _active_agent_run(self, session_id: str) -> tuple[Any, Any] | None:
        return None

    def _audit_step_tool_call(
        self,
        session_id: str,
        workflow: str,
        step: str,
        tool_name: str,
        result: str,
        *,
        reason: str | None = None,
        mcp_key: str | None = None,
    ) -> None:
        self.audited.append(result)


def _event(data: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=normalize_tool_fields(data),
    )


def _call_tool_event(tool_input: Any) -> HookEvent:
    return _event({"tool_name": "mcp__gobby__call_tool", "tool_input": tool_input})


@pytest.mark.parametrize(
    ("tool_input", "decision"),
    [(TRUNCATED_CALL, "block"), ({}, None)],
    ids=["marked-blocks", "empty-object-unchanged"],
)
def test_agent_mcp_block_list_treats_marked_call_tool_as_unknown(
    tool_input: Any, decision: str | None
) -> None:
    response = _Harness()._check_agent_tool_enforcement(
        _call_tool_event(tool_input),
        SESSION_ID,
        {"_agent_type": "reviewer", "_agent_blocked_mcp_tools": ["gobby-tasks:close_task"]},
    )

    assert (response.decision if response else None) == decision


@pytest.mark.parametrize(
    ("allowed_mcp_tools", "blocked_mcp_tools", "decision"),
    [
        (["gobby-skills:get_skill"], [], "block"),
        ("all", ["gobby-tasks:close_task"], "block"),
        ("all", [], None),
    ],
    ids=["allow-list", "block-list", "unrestricted"],
)
def test_step_mcp_restrictions_treat_marked_call_tool_as_unknown(
    allowed_mcp_tools: Any, blocked_mcp_tools: list[str], decision: str | None
) -> None:
    step = WorkflowStep.model_validate(
        {
            "name": "implement",
            "allowed_tools": ["mcp__gobby__call_tool"],
            "allowed_mcp_tools": allowed_mcp_tools,
            "blocked_mcp_tools": blocked_mcp_tools,
        }
    )
    harness = _Harness(step)

    response = harness._check_step_tool_enforcement_locked(
        _call_tool_event(TRUNCATED_CALL), SESSION_ID, {}
    )

    assert (response.decision if response else None) == decision
    assert harness.audited == (["block"] if decision else [])


@pytest.mark.parametrize(
    ("data", "conflict"),
    [
        ({"tool_name": "Bash", "tool_input": TRUNCATED_SHELL}, True),
        ({"tool_name": "Write", "tool_input": '{"file_path": "/repo/a.py", "con'}, False),
    ],
    ids=["marked-shell-blocks", "marked-write-not-a-commit"],
)
async def test_commit_guard_does_not_read_marked_shell_input_as_no_commit(
    data: dict[str, Any], conflict: bool
) -> None:
    reason = await foreign_staged_commit_conflict(
        MagicMock(),
        _event(data),
        session_id=SESSION_ID,
        project_id="project-1",
        project_path="/repo",
    )

    assert bool(reason) is conflict
    assert "-am wip" not in reason


def test_task_target_ignores_arguments_beside_a_marked_input() -> None:
    data = normalize_tool_fields(
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": TRUNCATED_CALL,
            "arguments": {"server_name": "gobby-tasks", "arguments": {"task_id": "#1"}},
        }
    )

    assert _target_task_tool_input(data) == {}
