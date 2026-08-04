"""Regression tests for dispatcher spawn isolation selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.spawn import _effective_spawn_isolation

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("agent_slug", ["planner", "plan-adversary"])
@pytest.mark.parametrize("task_isolation", ["worktree", "clone"])
def test_task_bound_planning_agents_force_none_isolation(
    agent_slug: str,
    task_isolation: str,
) -> None:
    action = SpawnAgentAction(
        task_id="7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
        task_ref="#1",
        agent_slug=agent_slug,
        prompt="go",
        initial_variables={"stage_name": "planning"},
    )
    task = SimpleNamespace(isolation=task_isolation)
    agent_body = SimpleNamespace(isolation="none")

    assert _effective_spawn_isolation(task=task, action=action, agent_body=agent_body) == "none"


@pytest.mark.parametrize(
    "stage_name",
    ["ideation", "research", "architecture", "prd", "planning", "expansion"],
)
@pytest.mark.parametrize("task_isolation", ["worktree", "clone"])
@pytest.mark.parametrize("agent_isolation", ["worktree", "clone"])
def test_pre_development_stages_force_none_isolation(
    stage_name: str,
    task_isolation: str,
    agent_isolation: str,
) -> None:
    action = SpawnAgentAction(
        task_id="7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
        task_ref="#1",
        agent_slug="planner",
        prompt="go",
        initial_variables={"stage_name": stage_name},
    )
    task = SimpleNamespace(isolation=task_isolation)
    agent_body = SimpleNamespace(isolation=agent_isolation)

    assert _effective_spawn_isolation(task=task, action=action, agent_body=agent_body) == "none"


def test_taskless_plan_adversary_forces_none_isolation() -> None:
    action = SpawnAgentAction(
        task_id="7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
        task_ref="#1",
        agent_slug="plan-adversary-taskless",
        prompt="go",
        initial_variables={"stage_name": "planning"},
    )
    task = SimpleNamespace(isolation="worktree")
    agent_body = SimpleNamespace(isolation="clone")

    assert _effective_spawn_isolation(task=task, action=action, agent_body=agent_body) == "none"


def test_taskless_plan_enhancer_forces_none_isolation() -> None:
    action = SpawnAgentAction(
        task_id="7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
        task_ref="#1",
        agent_slug="plan-enhancer-taskless",
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
        task_id="7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
        task_ref="#1",
        agent_slug="plan-adversary-taskless",
        prompt="go",
        initial_variables=RaisingInitialVariables({"stage_name": "development"}),
    )
    task = SimpleNamespace(isolation="clone")
    agent_body = SimpleNamespace(isolation="worktree")

    assert _effective_spawn_isolation(task=task, action=action, agent_body=agent_body) == "none"
