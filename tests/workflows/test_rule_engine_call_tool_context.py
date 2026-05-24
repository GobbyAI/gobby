"""Regression tests for call_tool evaluation context handling."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


def _make_event(data: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )


def test_call_tool_eval_context_does_not_mutate_target_arguments(db: HubDatabase) -> None:
    engine = RuleEngine(db)
    target_arguments = {"task_id": "#1", "commit_sha": "abc123"}
    event = _make_event(
        {
            "tool_name": "call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": target_arguments,
            },
        }
    )

    ctx = engine._build_eval_context(event, variables={})

    assert ctx["tool_input"] == {
        "task_id": "#1",
        "commit_sha": "abc123",
        "server_name": "gobby-tasks",
        "tool_name": "close_task",
    }
    assert event.data["tool_input"]["arguments"] == {
        "task_id": "#1",
        "commit_sha": "abc123",
    }
    assert target_arguments == {"task_id": "#1", "commit_sha": "abc123"}
