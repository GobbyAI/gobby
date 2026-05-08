"""Tests for ready-stage paths whose default agent is disabled or missing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _task(stage_name: str = "ideation"):
    stage = SimpleNamespace(name=stage_name, state="ready", position=0)
    return SimpleNamespace(id="task-1", ref="#1", task_type="task", stages=[stage])


def _context(stage_name: str, *, enabled: bool | None):
    agent_name = {
        "ideation": "analyst",
        "research": "researcher",
        "architecture": "architect",
        "prd": "product-manager",
    }[stage_name]
    workflow_row = None
    if enabled is not None:
        workflow_row = SimpleNamespace(name=agent_name, enabled=enabled)
    return SimpleNamespace(
        current_stage=SimpleNamespace(name=stage_name, state="ready"),
        stage_registry={
            stage_name: SimpleNamespace(
                name=stage_name,
                default_agent=agent_name,
                requires_human=False,
            )
        },
        agent_definitions={agent_name: workflow_row} if workflow_row is not None else {},
    )


def test_disabled_default_agent_treated_as_missing() -> None:
    from gobby.dispatch.rules import stage_agent_available  # noqa: PLC0415

    assert stage_agent_available(_context("ideation", enabled=True), "ideation") is True
    assert stage_agent_available(_context("ideation", enabled=False), "ideation") is False
    assert stage_agent_available(_context("ideation", enabled=None), "ideation") is False


@pytest.mark.parametrize(
    ("stage_name", "reason"),
    [
        ("ideation", "ideation_no_agent"),
        ("research", "research_no_agent"),
        ("architecture", "architecture_no_agent"),
        ("prd", "prd_no_agent"),
    ],
)
def test_disabled_default_agent_routes_to_stage_generic_escalation_rule(
    stage_name: str,
    reason: str,
) -> None:
    from gobby.dispatch.actions import EscalateAction
    from gobby.dispatch.rules import disabled_agent_escalation_rule

    action = disabled_agent_escalation_rule(
        _task(stage_name),
        _context(stage_name, enabled=False),
    )

    assert isinstance(action, EscalateAction)
    assert action.task_id == "task-1"
    assert action.reason == reason
