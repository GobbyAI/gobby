"""Tests for step-enforcement recovery behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.engine.enforcement_checks import (
    _DENIAL_COUNTS_VARIABLE,
    EnforcementCheckMixin,
)
from gobby.workflows.step_instances import AgentStepInstance

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
TASK_ID = "22222222-2222-4222-8222-222222222222"


class _EnforcementHarness(EnforcementCheckMixin):
    def __init__(self, step: WorkflowStep, instance: AgentStepInstance) -> None:
        self._step = step
        self._instance = instance
        self.instance_manager = MagicMock()
        self._task_manager = MagicMock()
        self._task_manager.get_task.return_value = SimpleNamespace(
            id=TASK_ID,
            seq_num=21710,
            path_cache="21678.21710",
            claimed_by_session_id=SESSION_ID,
        )
        self._run = SimpleNamespace(task_id=TASK_ID)
        self.audit_reasons: list[str] = []

    def _get_step_for_session(
        self, session_id: str
    ) -> tuple[WorkflowStep | None, AgentStepInstance | None]:
        assert session_id == SESSION_ID
        return self._step, self._instance

    def _active_agent_run(self, session_id: str) -> tuple[Any, Any] | None:
        assert session_id == SESSION_ID
        return self._run, MagicMock()

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
        del workflow, step, tool_name, mcp_key
        assert session_id == SESSION_ID
        assert result == "allow"
        if reason is not None:
            self.audit_reasons.append(reason)


def test_self_owned_claim_task_is_not_a_counted_denial() -> None:
    step = WorkflowStep.model_validate(
        {
            "name": "load_required_skills",
            "allowed_tools": ["mcp__gobby__call_tool"],
            "allowed_mcp_tools": ["gobby-skills:get_skill"],
        }
    )
    instance = MagicMock(spec=AgentStepInstance)
    instance.agent_name = "backend-developer"
    instance.variables = {}
    harness = _EnforcementHarness(step, instance)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "#21710"},
            },
        },
    )

    response = harness._check_step_tool_enforcement_locked(event, SESSION_ID, {})

    assert response is None
    assert _DENIAL_COUNTS_VARIABLE not in instance.variables
    assert harness.audit_reasons == ["claim_task targets the task already owned by this session"]
