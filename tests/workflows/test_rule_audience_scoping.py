"""Rule audience scoping: autonomous-audience rules skip interactive sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit


def _make_event(data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="11111111-1111-4111-8111-111111111111",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


@pytest.mark.asyncio
async def test_autonomous_audience_rules_skip_interactive_sessions(
    temp_db: HubDatabase,
) -> None:
    from gobby.storage.definitions.rules import RuleDefinitionManager

    manager = RuleDefinitionManager(temp_db)
    manager.create(
        name="autonomous-only",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            audience="autonomous",
            effects=[RuleEffect(type="block", tools=["Bash"], reason="autonomous only")],
        ).model_dump_json(),
        enabled=True,
        priority=10,
    )

    engine = RuleEngine(temp_db)
    event = _make_event({"tool_name": "Bash"})

    interactive = await engine.evaluate(
        event,
        session_id="11111111-1111-4111-8111-111111111111",
        variables={},
    )
    autonomous = await engine.evaluate(
        event,
        session_id="22222222-2222-4222-8222-222222222222",
        variables={"is_spawned_agent": True},
    )

    assert interactive.decision == "allow"
    assert autonomous.decision == "block"
