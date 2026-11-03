"""Generic review-result formatting and delivery."""

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.receipt_effects import worker_staging_scope
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from tests.fixtures.agent_definitions import seed_seat

pytestmark = pytest.mark.unit

EXTERNAL_SESSION_ID = "synthetic-provider-session"
PLATFORM_SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _event(data: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": PLATFORM_SESSION_ID},
    )


@pytest.mark.asyncio
async def test_class_recall_formatter_routing(temp_db: HubDatabase) -> None:
    body = RuleDefinitionBody(
        event=RuleTriggerEvent.BEFORE_TOOL,
        effects=[
            RuleEffect(
                type="mcp_call",
                server="gobby-review-learning",
                tool="recall_review_lessons_by_class",
                arguments={
                    "lesson_domain": "plan",
                    "lesson_types": ["missing-section"],
                },
                inject_result=True,
            )
        ],
    )
    RuleDefinitionManager(temp_db).create(
        name="test-class-recall-routing",
        definition_json=body.model_dump_json(),
        enabled=True,
    )
    seed_seat(temp_db, "custom-critic", include=["name:test-class-recall-routing"])

    async def dispatcher(
        server: str, tool: str, args: dict[str, Any], event: HookEvent
    ) -> dict[str, Any]:
        del server, tool, args, event
        return {
            "success": True,
            "result": {
                "count": 1,
                "lessons": [
                    {
                        "memory_id": "class-routing-lesson",
                        "pattern_id": "plan-review:missing-section:correctness:stale-section",
                        "do": "Check every required plan section.",
                        "avoid": "Leaving stale sections unreviewed.",
                    }
                ],
                "message": "ignored upstream rendering",
            },
        }

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
    with worker_staging_scope():
        first = await engine.evaluate(
            _event({"tool_name": "Read"}), EXTERNAL_SESSION_ID, {"_agent_type": "custom-critic"}
        )
        second = await engine.evaluate(
            _event({"tool_name": "Read"}), EXTERNAL_SESSION_ID, {"_agent_type": "custom-critic"}
        )

    assert first.context is not None
    assert "<review-guidance>" in first.context
    assert "matched lesson class" in first.context
    assert "Check every required plan section" in first.context
    assert second.context is None
