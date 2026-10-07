"""The protected-branch guard's operator override is never available to an agent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "block-landing-override"
SESSION_ID = "44444444-4444-4444-8444-444444444444"
GOBBY_PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"


def _bash_event(command: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=GOBBY_PROJECT_ID,
        data={"command": command, "tool_input": {"command": command}, "tool_name": "Bash"},
    )


@pytest.mark.parametrize(
    "variables",
    [{"is_spawned_agent": True}, {}],
    ids=["spawned", "interactive"],
)
async def test_landing_override_is_blocked_for_every_session(
    temp_db: HubDatabase, variables: dict[str, object]
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("DELETE FROM rule_definitions WHERE name != %s", (RULE_NAME,))
    engine = RuleEngine(temp_db)

    async def decide(command: str) -> tuple[str, str]:
        response = await engine.evaluate(
            _bash_event(command), session_id=SESSION_ID, variables=dict(variables)
        )
        return response.decision, response.reason or ""

    override = await decide("GOBBY_LAND_COMMIT=1 git -C ../main merge --ff-only lane")
    exported = await decide("export GOBBY_LAND_COMMIT=1 && git update-ref refs/heads/main lane")
    plain = await decide("git -C ../main merge --ff-only lane")

    assert override[0] == "block"
    assert "gobby-tasks-ops:land_commit" in override[1]
    assert exported[0] == "block"
    assert plain[0] == "allow"
