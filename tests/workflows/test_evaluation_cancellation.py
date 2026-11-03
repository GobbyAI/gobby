"""Cancellation fails open without applying effects; a resumed evaluation can complete."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import NoReturn
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_cancelled_evaluation_leaves_effect_for_resumed_evaluation(
    temp_db: HubDatabase,
) -> None:
    session_id = SessionManager(temp_db).register_session(
        external_id="synthetic-cancellation", source="claude", machine_id=None, project_id=None
    )
    RuleDefinitionManager(temp_db).create(
        name="synthetic-resume-marker",
        definition_json={
            "event": "turn_start",
            "effects": [{"type": "set_variable", "variable": "evaluation_finished", "value": True}],
        },
        tags=["default"],
    )
    runtime = MagicMock()
    runtime.is_closing = False
    handler = WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db),
        evaluation_runtime=runtime,
        config=SimpleNamespace(workflow=SimpleNamespace(enabled=True, timeout=5.0)),
    )
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="provider-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": session_id},
    )

    def cancel(
        coroutine: Coroutine[object, object, HookResponse], *, timeout: float | None = None
    ) -> NoReturn:
        coroutine.close()
        raise concurrent.futures.CancelledError

    runtime.run.side_effect = cancel
    with patch("asyncio.get_running_loop", side_effect=RuntimeError):
        cancelled = handler.evaluate(event)
    variables = SessionVariableManager(temp_db)
    assert cancelled.decision == "allow"
    assert not variables.get_variables(session_id).get("evaluation_finished")

    resumed = await handler._evaluate_rules(event)
    assert resumed.decision == "allow"
    assert variables.get_variables(session_id)["evaluation_finished"] is True
