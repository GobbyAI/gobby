"""Provider identity distinguishes native subagents from spawned sessions."""

from typing import Any

import pytest

from gobby.hooks.events import SessionSource
from gobby.workflows.engine.block_batching import operator_event_is_subagent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("source", "event_data", "variables", "expected"),
    [
        # A spawned Gobby agent is governed by its configured rules.
        (SessionSource.CLAUDE, {}, {"is_spawned_agent": True}, False),
        (SessionSource.CODEX, {}, {"is_spawned_agent": True}, False),
        # A payload-named native subagent is one on any CLI.
        (SessionSource.CLAUDE, {"agent_id": "agent-uuid"}, {"is_spawned_agent": True}, True),
        (SessionSource.CLAUDE, {"agent_id": "agent-uuid"}, {}, True),
        # Claude omits the key on the parent thread even while is_subagent is set.
        (SessionSource.CLAUDE, {}, {"is_subagent": True}, False),
        # CLIs without per-event identity keep the session flag.
        (SessionSource.CODEX, {}, {"is_subagent": True}, True),
    ],
)
def test_operator_event_is_subagent_ignores_spawned_agents(
    source: SessionSource,
    event_data: dict[str, Any],
    variables: dict[str, Any],
    expected: bool,
) -> None:
    assert operator_event_is_subagent(event_data, source, variables) is expected
