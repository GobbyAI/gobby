"""A rule declares retry recovery without acquiring authority from its name."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.blocked_tool_recovery import is_blocked_tool_recovery_remediation
from gobby.workflows.engine.core import RuleEngine
from tests.fixtures.agent_sessions import AgentSessions
from tests.workflows.rule_arming import install_retry_limit_rule

pytestmark = pytest.mark.unit


def test_recovery_condition_requires_a_block_effect() -> None:
    with pytest.raises(ValidationError, match="recovery_when"):
        RuleEffect(type="set_variable", variable="ready", value=True, recovery_when="True")


def test_recovery_condition_requires_a_before_tool_rule() -> None:
    with pytest.raises(ValidationError, match="recovery_when"):
        RuleDefinitionBody.model_validate(
            {
                "event": "turn_end",
                "effects": [{"type": "block", "reason": "Wait.", "recovery_when": "True"}],
            }
        )


@pytest.mark.parametrize("condition", ["event.missing(", "event['absent']", "__import__('os')"])
def test_invalid_recovery_condition_preserves_the_retry_budget(condition: str) -> None:
    variables = {"_last_blocked_recovery_conditions": [condition], "consecutive_tool_blocks": 4}
    assert not is_blocked_tool_recovery_remediation(variables, {"repair_ready": True})
    assert variables["consecutive_tool_blocks"] == 4


def _event(session_id: str, project_id: str, *, repair_ready: bool) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        project_id=project_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "Bash",
            "tool_input": {"command": "gcode outline src/gobby/workflows/engine/core.py"},
            "repair_ready": repair_ready,
        },
    )


@pytest.mark.parametrize("rule_name", ["fixture-recovery", "fixture-definition-b0e4868c"])
@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("blocked_again", [False, True])
@pytest.mark.asyncio
async def test_retry_recovery_uses_the_block_declaration_not_its_name(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    rule_name: str,
    declared: bool,
    blocked_again: bool,
) -> None:
    install_retry_limit_rule(temp_db)
    author = AgentSessions(temp_db, sample_project["id"]).add(
        "fixture-recovery-seat",
        definition_fields={"workflows": {"rule_selectors": {"include": ["tag:retry-policy"]}}},
    )
    effect: dict[str, Any] = {"type": "block", "reason": "Satisfy the declared repair."}
    if declared:
        effect["recovery_when"] = "event.get('repair_ready') == True"
    RuleDefinitionManager(temp_db).create(
        rule_name,
        {
            "event": "before_tool",
            "when": "not event.data.get('repair_ready')",
            "effects": [effect],
        },
        project_id=sample_project["id"],
        tags=["retry-policy"],
    )
    if blocked_again:
        RuleDefinitionManager(temp_db).create(
            "fixture-additional-policy",
            {
                "event": "before_tool",
                "when": "event.data.get('repair_ready')",
                "effects": [{"type": "block", "reason": "Additional approval needed."}],
            },
            project_id=sample_project["id"],
            tags=["retry-policy"],
        )
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {
        "_agent_type": "fixture-recovery-seat",
        "project": {"id": sample_project["id"], "path": str(tmp_path)},
    }
    blocked = await engine.evaluate(
        _event(author, sample_project["id"], repair_ready=False), author, variables
    )
    assert blocked.decision == "block"
    variables["consecutive_tool_blocks"] = 4
    variables["max_consecutive_blocked_tool_attempts"] = 5
    recovered = await engine.evaluate(
        _event(author, sample_project["id"], repair_ready=True), author, variables
    )
    assert recovered.decision == ("allow" if declared and not blocked_again else "block")
    if declared:
        assert variables["consecutive_tool_blocks"] == 0
        if blocked_again:
            assert "Additional approval needed." in (recovered.reason or "")
        else:
            assert variables["_last_blocked_tool"] == ""
