"""Regression tests for dispatcher spawn isolation selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.spawn import _effective_spawn_isolation

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("agent_slug", ["planner", "plan-adversary", "plan-adversary-taskless"])
def test_main_context_agents_force_none_isolation(agent_slug: str) -> None:
    action = SpawnAgentAction(
        task_id="task-1",
        task_ref="#1",
        agent_slug=agent_slug,
        prompt="go",
        initial_variables={"stage_name": "development"},
    )
    task = SimpleNamespace(isolation="clone")
    agent_body = SimpleNamespace(isolation="worktree")

    assert _effective_spawn_isolation(task=task, action=action, agent_body=agent_body) == "none"


class RaisingInitialVariables(dict[str, object]):
    def get(self, _key: object, _default: object = None) -> object:
        raise AssertionError("main-context isolation should bypass stage lookup")


def test_main_context_isolation_bypasses_stage_lookup() -> None:
    action = SpawnAgentAction(
        task_id="task-1",
        task_ref="#1",
        agent_slug="planner",
        prompt="go",
        initial_variables=RaisingInitialVariables({"stage_name": "development"}),
    )
    task = SimpleNamespace(isolation="clone")
    agent_body = SimpleNamespace(isolation="worktree")

    assert _effective_spawn_isolation(task=task, action=action, agent_body=agent_body) == "none"
