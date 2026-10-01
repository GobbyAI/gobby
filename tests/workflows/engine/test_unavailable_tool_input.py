"""Rules that read a marked-unavailable tool input fail by effect, never see {} (#23168)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "22222222-2222-4222-8222-222222222222"
TRUNCATED = '{"file_path": "/repo/src/app.py", "content": "VALUE = '


def _event(tool_input: Any) -> HookEvent:
    data = normalize_tool_fields({"tool_name": "Write", "tool_input": tool_input})
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )


async def _evaluate(
    db: HubDatabase, when: str, effect: RuleEffect, event: HookEvent
) -> HookResponse:
    RuleDefinitionManager(db).create(
        name="tool-input-rule",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL, when=when, effects=[effect]
        ).model_dump_json(),
        priority=10,
        enabled=True,
    )
    return await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})


BLOCK = RuleEffect(type="block", reason="protected path")
INJECT = RuleEffect(type="inject_context", template="tool input rule fired")


@pytest.mark.parametrize(
    "when",
    [
        "tool_input.get('file_path') == '/repo/src/app.py'",
        "tool_input.file_path == '/repo/src/app.py'",
        "'file_path' in tool_input",
        "isinstance(tool_input, dict) and tool_input.get('file_path') == '/repo/src/app.py'",
    ],
    ids=["get", "attribute", "contains", "isinstance-guarded"],
)
async def test_block_rule_reading_unavailable_input_blocks(temp_db: HubDatabase, when: str) -> None:
    response = await _evaluate(temp_db, when, BLOCK, _event(TRUNCATED))

    assert response.decision == "block"


async def test_block_rule_that_ignores_tool_input_is_unaffected(temp_db: HubDatabase) -> None:
    response = await _evaluate(
        temp_db, "event.data.get('tool_name') == 'Bash'", BLOCK, _event(TRUNCATED)
    )

    assert response.decision == "allow"


@pytest.mark.parametrize(
    ("tool_input", "fired"),
    [({"file_path": "/repo/src/app.py"}, True), (TRUNCATED, False)],
    ids=["readable-input-fires", "unavailable-input-fails-open"],
)
async def test_non_block_effect_reading_unavailable_input_fails_open(
    temp_db: HubDatabase, tool_input: Any, fired: bool
) -> None:
    response = await _evaluate(
        temp_db, "tool_input.get('file_path') == '/repo/src/app.py'", INJECT, _event(tool_input)
    )

    assert response.decision == "allow"
    assert ("tool input rule fired" in (response.context or "")) is fired
