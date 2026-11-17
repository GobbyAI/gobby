"""Bundled policy selects native hosting without dropping recovery gates."""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.rule_validation_yaml import normalize_rule_yaml_entry
from tests.workflows.test_subagent_handoff_exemption import _bundled_rules, _engine, _session_tool

pytestmark = pytest.mark.unit
SESSION_ID = "11111111-1111-4111-8111-111111111111"
_PRESSURE = ("context_compact_guidance_message", "context_compact_mid_turn_pressure_band")
_TERMINAL_STATE = (*_PRESSURE, "native_terminal_hosted", "handoff", "context_compact")


def _all_rules() -> list[tuple[str, dict[str, Any]]]:
    rules = []
    for label, rule in _bundled_rules():
        body_dict, _metadata = normalize_rule_yaml_entry(rule)
        body = RuleDefinitionBody.model_validate(body_dict)
        rules.append((label, {**rule, "effects": [e.model_dump() for e in body.resolved_effects]}))
    return rules


def _event(kind: HookEventType) -> HookEvent:
    return HookEvent(
        event_type=kind,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Read", "tool_input": {"file_path": "/repo/example.py"}},
    )


def _variables(hosted: bool) -> dict[str, Any]:
    return {
        "native_terminal_hosted": hosted,
        "context_compact_guidance_kind": "block",
        "context_compact_guidance_message": "Context pressure guidance",
        "context_compact_mid_turn_pressure_band": "block",
        "context_compact_block_message": "Context pressure block",
        "mode_level": 2,
    }


@pytest.mark.parametrize(
    "kind", [HookEventType.BEFORE_TOOL, HookEventType.BEFORE_AGENT, HookEventType.AFTER_TOOL]
)
async def test_bundled_rules_skip_terminal_control_when_unhosted(
    temp_db: HubDatabase, kind: HookEventType
) -> None:
    rules = [r for r in _all_rules() if any(key in str(r[1].get("when")) for key in _PRESSURE)]
    assert rules
    engine = _engine(temp_db, rules)
    hosted = await engine.evaluate(_event(kind), SESSION_ID, _variables(True))
    unhosted = await engine.evaluate(_event(kind), SESSION_ID, _variables(False))
    assert hosted.decision == "block" if kind == HookEventType.BEFORE_TOOL else hosted.context
    assert unhosted.decision == "allow"
    assert not unhosted.context


@pytest.mark.parametrize(
    "kind", [HookEventType.BEFORE_TOOL, HookEventType.BEFORE_AGENT, HookEventType.AFTER_AGENT]
)
async def test_unhosted_session_keeps_non_terminal_rules(
    temp_db: HubDatabase, kind: HookEventType
) -> None:
    rules = [
        r for r in _all_rules() if not any(key in str(r[1].get("when")) for key in _TERMINAL_STATE)
    ]
    assert rules
    engine = _engine(temp_db, rules)
    hosted = await engine.evaluate(_event(kind), SESSION_ID, deepcopy(_variables(True)))
    unhosted = await engine.evaluate(_event(kind), SESSION_ID, deepcopy(_variables(False)))
    assert (hosted.decision, hosted.reason, hosted.context) == (
        unhosted.decision,
        unhosted.reason,
        unhosted.context,
    )


async def test_pending_pull_still_blocks_an_unhosted_session(temp_db: HubDatabase) -> None:
    rules = [r for r in _all_rules() if "handoff_pull_pending" in str(r[1].get("when"))]
    assert rules
    engine = _engine(temp_db, rules)
    state = {"native_terminal_hosted": False, "handoff_pull_pending": True}
    response = await engine.evaluate(_event(HookEventType.BEFORE_TOOL), SESSION_ID, state)
    assert response.decision == "block"
    pull = await engine.evaluate(_session_tool("get_handoff", subagent=False), SESSION_ID, state)
    assert pull.decision == "allow"
