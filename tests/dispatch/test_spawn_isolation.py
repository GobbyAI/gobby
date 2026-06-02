"""Regression tests for dispatcher spawn isolation selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.spawn import _effective_spawn_isolation

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("agent_slug", ["planner", "plan-adversary"])
@pytest.mark.parametrize("task_isolation", ["worktree", "clone"])
def test_task_bound_planning_agents_inherit_task_isolation(
    agent_slug: str,
    task_isolation: str,
) -> None:
    action = SpawnAgentAction(
        task_id="task-1",
        task_ref="#1",
        agent_slug=agent_slug,
        prompt="go",
        initial_variables={"stage_name": "planning"},
    )
    task = SimpleNamespace(isolation=task_isolation)
    agent_body = SimpleNamespace(isolation="none")

    assert (
        _effective_spawn_isolation(task=task, action=action, agent_body=agent_body)
        == task_isolation
    )


@pytest.mark.parametrize("stage_name", ["planning", "expansion"])
def test_pre_development_stages_default_to_none_without_task_isolation(stage_name: str) -> None:
    action = SpawnAgentAction(
        task_id="task-1",
        task_ref="#1",
        agent_slug="planner",
        prompt="go",
        initial_variables={"stage_name": stage_name},
    )
    task = SimpleNamespace(isolation="none")
    agent_body = SimpleNamespace(isolation="worktree")

    assert _effective_spawn_isolation(task=task, action=action, agent_body=agent_body) == "none"


def test_taskless_plan_adversary_forces_none_isolation() -> None:
    action = SpawnAgentAction(
        task_id="task-1",
        task_ref="#1",
        agent_slug="plan-adversary-taskless",
        prompt="go",
        initial_variables={"stage_name": "planning"},
    )
    task = SimpleNamespace(isolation="worktree")
    agent_body = SimpleNamespace(isolation="clone")

    assert _effective_spawn_isolation(task=task, action=action, agent_body=agent_body) == "none"


class RaisingInitialVariables(dict[str, object]):
    def get(self, _key: object, _default: object = None) -> object:
        raise AssertionError("main-context isolation should bypass stage lookup")


def test_main_context_isolation_bypasses_stage_lookup() -> None:
    action = SpawnAgentAction(
        task_id="task-1",
        task_ref="#1",
        agent_slug="plan-adversary-taskless",
        prompt="go",
        initial_variables=RaisingInitialVariables({"stage_name": "development"}),
    )
    task = SimpleNamespace(isolation="clone")
    agent_body = SimpleNamespace(isolation="worktree")

    assert _effective_spawn_isolation(task=task, action=action, agent_body=agent_body) == "none"
