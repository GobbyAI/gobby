"""Approving unowned dirty paths is the operator's decision; no seat may run the approval."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "block-dirty-path-approval-cli"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
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
async def test_dirty_path_approval_cli_is_blocked_for_every_session(
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

    direct = await decide("gobby sessions approve-dirty-paths #7 --checkout /repo a.txt")
    via_uv = await decide("cd /repo && uv run gobby sessions approve-dirty-paths #7 a.txt")
    substituted = await decide('echo "$(gobby sessions approve-dirty-paths #7 a.txt)"')
    quoted = await decide('echo "ask the operator: gobby sessions approve-dirty-paths #7 a.txt"')
    listing = await decide("gobby sessions list")

    assert direct[0] == "block"
    assert "operator" in direct[1]
    assert via_uv[0] == "block"
    assert substituted[0] == "block"
    assert quoted[0] == "allow"
    assert listing[0] == "allow"
