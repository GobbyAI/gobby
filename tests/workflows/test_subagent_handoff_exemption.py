"""Parent-turn handoff gates leave Claude subagent tool calls alone.

Claude Code subagents run inside their parent's Gobby session, so session-level
handoff and context-pressure state belongs to the parent's main thread. The hook
payload's ``agent_id`` marks a subagent-issued event. The session-wide
``is_subagent`` flag stays set while the main thread runs too, so it cannot
decide this. Bundled policy runs here under synthetic rule names, selected by
the state its conditions read.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
SUBAGENT_ID = "a73246e920a8c3bd5"

# A condition that positively reads the parent's handoff or context-pressure state.
_PARENT_TURN_STATE = re.compile(
    r"(?<!not )variables\.get\('(?:handoff_pull_pending|context_compact_handoff_result"
    r"|context_compact_mid_turn_pressure_band|context_compact_guidance_message)'\)"
)
# A condition that fires only for a subagent-issued event.
_SUBAGENT_EVENT = re.compile(r"(?<!not )event\.data\.get\('agent_id'\)")
_TOOL_EVENTS = {"before_tool": HookEventType.BEFORE_TOOL, "after_tool": HookEventType.AFTER_TOOL}

# Every parent-turn gate armed at once: staged and failed delivery, a pending
# pull and the block band.
_ARMED_PARENT_STATE: dict[str, Any] = {
    "handoff_pull_pending": True,
    "context_compact_handoff_result": {
        "delivery_pending": True,
        "attempt_pending": True,
        "delivery_failed": True,
    },
    "context_compact_mid_turn_pressure_band": "block",
    "context_compact_block_message": "Context limit reached.",
    "context_compact_guidance_message": "Context pressure is high.",
    "context_compact_guidance_kind": "block",
}

BundledRule = tuple[str, dict[str, Any]]


def _bundled_rules() -> list[BundledRule]:
    rules: list[BundledRule] = []
    for path in sorted(get_bundled_rules_path().rglob("*.yaml")):
        if "deprecated" in path.parts:
            continue
        document = yaml.safe_load(path.read_text())
        if not isinstance(document, dict):
            continue
        for name, rule in (document.get("rules") or {}).items():
            if isinstance(rule, dict) and rule.get("enabled", True):
                rules.append((f"{path.parent.name}/{name}", rule))
    return rules


def _parent_turn_gates() -> list[BundledRule]:
    return [
        (label, rule)
        for label, rule in _bundled_rules()
        if rule.get("event") in _TOOL_EVENTS and _PARENT_TURN_STATE.search(str(rule.get("when")))
    ]


def _subagent_only_gates() -> list[BundledRule]:
    return [
        (label, rule)
        for label, rule in _bundled_rules()
        if rule.get("event") == "before_tool" and _SUBAGENT_EVENT.search(str(rule.get("when")))
    ]


def _engine(db: HubDatabase, rules: list[BundledRule]) -> RuleEngine:
    manager = RuleDefinitionManager(db)
    for index, (_label, rule) in enumerate(rules):
        body = RuleDefinitionBody.model_validate(
            {key: rule[key] for key in ("event", "when", "effects") if key in rule}
        )
        manager.create(
            name=f"synthetic-handoff-gate-{index}",
            tags=["default"],
            priority=int(rule.get("priority", 100)),
            definition_json=body.model_dump(mode="json"),
        )
    return RuleEngine(db)


def _event(event_type: HookEventType, data: dict[str, Any], *, subagent: bool) -> HookEvent:
    payload = dict(data)
    if subagent:
        payload |= {"agent_id": SUBAGENT_ID, "agent_type": "general-purpose"}
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=payload,
    )


def _write(event_type: HookEventType, *, subagent: bool) -> HookEvent:
    return _event(
        event_type,
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/report.md", "content": "findings"},
        },
        subagent=subagent,
    )


def _session_tool(tool: str, *, subagent: bool) -> HookEvent:
    return _event(
        HookEventType.BEFORE_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": "gobby-sessions",
            "mcp_tool": tool,
            "tool_input": {"server_name": "gobby-sessions", "tool_name": tool, "arguments": {}},
        },
        subagent=subagent,
    )


def test_selection_covers_post_handoff_and_pull_gates() -> None:
    conditions = [str(rule.get("when")) for _label, rule in _parent_turn_gates()]
    assert any("context_compact_handoff_result" in condition for condition in conditions)
    assert any("handoff_pull_pending" in condition for condition in conditions)
    assert _subagent_only_gates(), "a bundled gate must refuse subagent handoff calls"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "rule"),
    [pytest.param(*gate, id=gate[0]) for gate in _parent_turn_gates()],
)
async def test_parent_turn_gate_holds_main_thread_and_skips_subagent(
    temp_db: HubDatabase, label: str, rule: dict[str, Any]
) -> None:
    engine = _engine(temp_db, [(label, rule)])
    event_type = _TOOL_EVENTS[str(rule["event"])]

    main = await engine.evaluate(
        _write(event_type, subagent=False),
        session_id=SESSION_ID,
        variables=dict(_ARMED_PARENT_STATE),
    )
    subagent = await engine.evaluate(
        _write(event_type, subagent=True),
        session_id=SESSION_ID,
        variables=dict(_ARMED_PARENT_STATE),
    )

    if event_type == HookEventType.BEFORE_TOOL:
        assert main.decision == "block", f"{label} must still gate the parent's main thread"
    else:
        assert main.context, f"{label} must still reach the parent's main thread"
    assert subagent.decision == "allow", f"{label} blocked a subagent: {subagent.reason}"
    assert not subagent.context, f"{label} told a subagent about the parent's handoff"


@pytest.mark.asyncio
async def test_subagent_cannot_consume_or_stage_the_parent_handoff(
    temp_db: HubDatabase,
) -> None:
    engine = _engine(temp_db, _parent_turn_gates() + _subagent_only_gates())
    pulled = {"handoff_pull_pending": True}

    main_pull = await engine.evaluate(
        _session_tool("get_handoff", subagent=False), session_id=SESSION_ID, variables=pulled
    )
    subagent_pull = await engine.evaluate(
        _session_tool("get_handoff", subagent=True), session_id=SESSION_ID, variables=pulled
    )
    subagent_stage = await engine.evaluate(
        _session_tool("set_handoff", subagent=True), session_id=SESSION_ID, variables={}
    )
    subagent_work = await engine.evaluate(
        _write(HookEventType.BEFORE_TOOL, subagent=True), session_id=SESSION_ID, variables=pulled
    )

    assert main_pull.decision == "allow", main_pull.reason
    assert subagent_pull.decision == "block"
    assert "parent" in (subagent_pull.reason or "")
    assert subagent_stage.decision == "block"
    assert subagent_work.decision == "allow", subagent_work.reason
