"""One authoritative wait lookup supplies every selected rule in a pass."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import PipelineSubscriberStorageError
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_fails", [False, True])
async def test_active_wait_is_shared_and_storage_failure_keeps_rules_armed(
    temp_db: HubDatabase, caplog: pytest.LogCaptureFixture, lookup_fails: bool
) -> None:
    names = ["synthetic-first-wait", "synthetic-second-wait"]
    AgentDefinitionManager(temp_db).create(
        "wait-reader",
        {
            "name": "wait-reader",
            "prompts": {"agent": "Wait for a peer."},
            "workflows": {"rule_selectors": {"include": names}},
        },
    )
    for name in names:
        RuleDefinitionManager(temp_db).create(
            name,
            {
                "event": "turn_end",
                "when": "not has_active_agent_wait()",
                "effects": [{"type": "block", "reason": name}],
            },
        )
    session = "11111111-1111-4111-8111-111111111111"
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id=session,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={},
    )
    with patch("gobby.workflows.engine.core.CompletionSubscriberManager") as manager:
        lookup = manager.return_value.has_active_agent_wait
        lookup.return_value = True
        if lookup_fails:
            lookup.side_effect = PipelineSubscriberStorageError("lookup failed")
        with caplog.at_level("WARNING"):
            response = await RuleEngine(temp_db).evaluate(
                event,
                session_id=session,
                variables={"_agent_type": "wait-reader"},
            )
    lookup.assert_called_once_with(session)
    assert response.decision == ("block" if lookup_fails else "allow")
    if lookup_fails:
        assert "Failed to determine active agent wait" in caplog.text
