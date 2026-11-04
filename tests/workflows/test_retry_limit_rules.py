"""Same-tool retry escalation is a rule marked ``retry_limit``.

The engine charges each repeated denial of one tool to the blocked-attempt
counter. A retry-limit rule reads that counter through ``block_state`` and owns
the threshold and the wording. The engine evaluates it before agent and step
enforcement and before every other rule, so its block preempts them and leaves
the original recovery state intact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from tests.fixtures.agent_definitions import seed_seat
from tests.workflows.rule_arming import (
    SYNTHETIC_ESCALATION_PREFIX,
    SYNTHETIC_RETRY_LIMIT_RULE,
    arm_bundled_retry_limit_rules,
    install_retry_limit_rule,
)

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
ENFORCED_SEAT = "fixture-retry-seat"
BLOCKED_RULE = "fixture-todowrite-gate"
BLOCKED_REASON = "Rule enforced by Gobby: [fixture-todowrite-gate]\nUse gobby-tasks instead"


def _blocked_state(**extra: Any) -> dict[str, Any]:
    """Recovery state an earlier gate left behind after blocking TodoWrite."""
    return {
        "_last_blocked_tool": "TodoWrite",
        "_last_blocked_rule_name": BLOCKED_RULE,
        "_last_blocked_reason": BLOCKED_REASON,
        **extra,
    }


def _retry_event() -> HookEvent:
    # Codex has no tool-batch boundary, so every retry is charged.
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": "TodoWrite"},
    )


def _install_sibling_gate(db: HubDatabase) -> None:
    """A later rule that both sets a variable and blocks TodoWrite."""
    RuleDefinitionManager(db).create(
        name="fixture-sibling-gate",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            mandatory=True,
            effects=[
                RuleEffect(type="set_variable", variable="sibling_ran", value=True),
                RuleEffect(type="block", tools=["TodoWrite"], reason="Sibling gate text"),
            ],
        ).model_dump_json(),
        enabled=True,
        priority=50,
    )


@pytest.mark.asyncio
async def test_retry_limit_rule_escalates_at_its_threshold(temp_db: HubDatabase) -> None:
    install_retry_limit_rule(temp_db)
    engine = RuleEngine(temp_db)
    variables = _blocked_state(max_consecutive_blocked_tool_attempts=3)

    first = await engine.evaluate(_retry_event(), SESSION_ID, variables)
    assert first.decision == "allow"
    variables.update(_blocked_state())

    second = await engine.evaluate(_retry_event(), SESSION_ID, variables)

    assert second.decision == "block"
    assert second.reason is not None
    assert second.reason.startswith(f"Rule enforced by Gobby: [{SYNTHETIC_RETRY_LIMIT_RULE}]\n")
    assert f"{SYNTHETIC_ESCALATION_PREFIX}: TodoWrite x3 | {BLOCKED_REASON}" in second.reason


@pytest.mark.asyncio
async def test_without_a_retry_limit_rule_retries_never_escalate(temp_db: HubDatabase) -> None:
    """The threshold lives in the rule: with none installed, the engine only counts."""
    engine = RuleEngine(temp_db)
    variables = _blocked_state(max_consecutive_blocked_tool_attempts=2)

    for expected_count in (1, 2, 3):
        response = await engine.evaluate(_retry_event(), SESSION_ID, variables)
        assert response.decision == "allow"
        assert variables["consecutive_tool_blocks"] == expected_count
        variables.update(_blocked_state())


@pytest.mark.asyncio
async def test_escalation_supersedes_sibling_gates_and_their_side_effects(
    temp_db: HubDatabase,
) -> None:
    install_retry_limit_rule(temp_db)
    _install_sibling_gate(temp_db)
    engine = RuleEngine(temp_db)
    variables = _blocked_state(consecutive_tool_blocks=3, max_consecutive_blocked_tool_attempts=5)

    response = await engine.evaluate(_retry_event(), SESSION_ID, variables)

    assert response.decision == "block"
    assert response.reason is not None
    assert SYNTHETIC_ESCALATION_PREFIX in response.reason
    assert "Sibling gate text" not in response.reason
    assert variables.get("sibling_ran") is None


@pytest.mark.asyncio
async def test_escalation_keeps_the_original_recovery_state(temp_db: HubDatabase) -> None:
    """The escalation is not a new original block, so the next one still quotes the first."""
    install_retry_limit_rule(temp_db)
    engine = RuleEngine(temp_db)
    variables = _blocked_state(consecutive_tool_blocks=3, max_consecutive_blocked_tool_attempts=5)

    first = await engine.evaluate(_retry_event(), SESSION_ID, variables)
    second = await engine.evaluate(_retry_event(), SESSION_ID, variables)

    assert first.decision == second.decision == "block"
    assert variables["_last_blocked_tool"] == "TodoWrite"
    assert variables["_last_blocked_rule_name"] == BLOCKED_RULE
    assert variables["_last_blocked_reason"] == BLOCKED_REASON
    assert second.reason is not None
    assert BLOCKED_REASON in second.reason


@pytest.mark.asyncio
async def test_escalation_preempts_agent_tool_enforcement(temp_db: HubDatabase) -> None:
    """An enforcement block that keeps repeating still reaches the retry limit."""
    seed_seat(temp_db, ENFORCED_SEAT, include=["*"])
    install_retry_limit_rule(temp_db)
    engine = RuleEngine(temp_db)
    variables = _blocked_state(
        _agent_type=ENFORCED_SEAT,
        _agent_blocked_tools=["TodoWrite"],
        consecutive_tool_blocks=3,
        max_consecutive_blocked_tool_attempts=5,
    )

    response = await engine.evaluate(_retry_event(), SESSION_ID, variables)

    assert response.decision == "block"
    assert response.reason is not None
    assert response.reason.startswith(f"Rule enforced by Gobby: [{SYNTHETIC_RETRY_LIMIT_RULE}]\n")
    assert variables["_last_blocked_rule_name"] == BLOCKED_RULE


@pytest.mark.asyncio
async def test_bundled_retry_limit_rules_escalate_with_the_original_reason(
    temp_db: HubDatabase,
) -> None:
    armed = arm_bundled_retry_limit_rules(temp_db)
    assert armed, "the bundled rules must ship a retry-limit escalation"
    engine = RuleEngine(temp_db)
    variables = _blocked_state(consecutive_tool_blocks=1, max_consecutive_blocked_tool_attempts=3)

    response = await engine.evaluate(_retry_event(), SESSION_ID, variables)

    assert response.decision == "block"
    assert response.reason is not None
    assert any(response.reason.startswith(f"Rule enforced by Gobby: [{name}]\n") for name in armed)
    assert "You have attempted TodoWrite 3 times consecutively" in response.reason
    assert f"Most recent original block reason:\n{BLOCKED_REASON}" in response.reason
    assert "Recovery required:" in response.reason


@pytest.mark.asyncio
async def test_bundled_retry_limit_rules_say_so_when_no_reason_was_kept(
    temp_db: HubDatabase,
) -> None:
    assert arm_bundled_retry_limit_rules(temp_db)
    engine = RuleEngine(temp_db)
    variables = _blocked_state(
        _last_blocked_reason="",
        consecutive_tool_blocks=1,
        max_consecutive_blocked_tool_attempts=2,
    )

    response = await engine.evaluate(_retry_event(), SESSION_ID, variables)

    assert response.decision == "block"
    assert response.reason is not None
    assert "Most recent original block reason:" not in response.reason
    assert "The prior block reason was unavailable." in response.reason
    assert "Scroll up and read the immediately preceding tool error" in response.reason


@pytest.mark.parametrize("event", sorted(set(RuleTriggerEvent) - {RuleTriggerEvent.BEFORE_TOOL}))
def test_retry_limit_is_restricted_to_before_tool_rules(event: RuleTriggerEvent) -> None:
    with pytest.raises(ValueError, match="retry_limit is restricted to before_tool"):
        RuleDefinitionBody(
            event=event,
            retry_limit=True,
            effects=[RuleEffect(type="block", reason="Stop retrying.")],
        )
