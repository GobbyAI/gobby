"""Coverage for concise repeat-block rendering."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.blocked_tool_recovery import (
    _ACTION_WORD_RE as ACTION_WORD_RE,
)
from gobby.workflows.engine.blocked_tool_recovery import (
    _BACKTICK_FORM_RE as BACKTICK_FORM_RE,
)
from gobby.workflows.engine.blocked_tool_recovery import (
    _CALL_FORM_RE as CALL_FORM_RE,
)
from gobby.workflows.engine.blocked_tool_recovery import (
    _balanced_call_end,
)
from gobby.workflows.engine.core import RuleEngine

SESSION_ID = str(uuid.uuid4())
RULES_ROOT = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/rules"
TERSE_REASON = (
    "Rule enforced by Gobby: [repeat-block] (full reason shown earlier this turn — scroll up)."
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _make_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Read"},
    )


def _insert_block_rule(
    manager: RuleDefinitionManager,
    reason: str,
) -> None:
    manager.create(
        name="repeat-block",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason=reason)],
        ).model_dump_json(),
        priority=100,
        enabled=True,
    )


async def _evaluate_twice(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    reason: str,
) -> str:
    _insert_block_rule(manager, reason)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}
    event = _make_event()

    await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    second = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

    assert second.decision == "block"
    assert second.reason is not None
    return second.reason


@pytest.mark.asyncio
async def test_collapsed_reason_keeps_directive(
    db: HubDatabase,
    manager: RuleDefinitionManager,
) -> None:
    command = (
        'call_tool("gobby-memory", "get_recall_memories", {"recall_request_id":"request-123"})'
    )

    collapsed = await _evaluate_twice(
        db,
        manager,
        f"Retrieve the pending memories: {command}, then continue.",
    )

    assert collapsed.startswith(TERSE_REASON)
    assert command in collapsed
    assert collapsed.count("\n") == 1


@pytest.mark.asyncio
async def test_no_directive_collapses_clean(
    db: HubDatabase,
    manager: RuleDefinitionManager,
) -> None:
    collapsed = await _evaluate_twice(
        db,
        manager,
        "This operation remains blocked pending review.",
    )

    assert collapsed == TERSE_REASON


def _enabled_actionable_reasons() -> list[tuple[str, str]]:
    inventory: list[tuple[str, str]] = []
    for path in sorted(RULES_ROOT.rglob("*.yaml")):
        document = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
        for rule_name, rule in document.get("rules", {}).items():
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            for effect in rule.get("effects", []):
                if not isinstance(effect, dict) or effect.get("type") != "block":
                    continue
                reason = effect.get("reason")
                if isinstance(reason, str) and ACTION_WORD_RE.search(reason):
                    inventory.append((str(rule_name), reason))
    return inventory


def _assert_complete_calls(directive: str) -> None:
    for match in CALL_FORM_RE.finditer(directive):
        assert _balanced_call_end(directive, match.start()) is not None, directive


def test_directive_forms_survive_collapse() -> None:
    from gobby.workflows.engine.blocked_tool_recovery import recovery_directive_suffix

    inventory = _enabled_actionable_reasons()
    assert inventory

    # A corpus survey: it can only require forms that some enabled bundled rule
    # actually uses. "set_variable" was dropped when 66dbca284 (#19408) retired
    # require-memory-review-before-status, the only rule whose block reason
    # carried a set_variable(...) directive. The collapse logic for it is still
    # exercised below whenever a reason reintroduces the form.
    forms = {
        "single_line_call_tool": False,
        "multiline_call_tool": False,
        "direct_mcp_tool": False,
        "backticked_command": False,
        "alternative_commands": False,
    }
    for rule_name, reason in inventory:
        call_matches = list(CALL_FORM_RE.finditer(reason))
        backtick_matches = list(BACKTICK_FORM_RE.finditer(reason))
        suffix = recovery_directive_suffix(reason)

        assert suffix, rule_name
        assert suffix.startswith("\nRecovery directive: "), rule_name
        assert suffix.count("\n") == 1, rule_name
        _assert_complete_calls(suffix)

        if call_matches:
            for match in call_matches:
                call_end = _balanced_call_end(reason, match.start())
                assert call_end is not None, rule_name
                call_text = reason[match.start() : call_end]
                assert " ".join(call_text.split()) in suffix, rule_name
        for match in backtick_matches:
            assert match.group() in suffix, rule_name

        for match in call_matches:
            call_end = _balanced_call_end(reason, match.start())
            assert call_end is not None, rule_name
            call_text = reason[match.start() : call_end]
            if call_text.startswith("call_tool"):
                if "\n" in call_text:
                    forms["multiline_call_tool"] = True
                else:
                    forms["single_line_call_tool"] = True
            elif not call_text.startswith(("get_tool_schema", "set_variable")):
                forms["direct_mcp_tool"] = True
            if call_text.startswith("set_variable"):
                forms["set_variable"] = True

        if backtick_matches:
            forms["backticked_command"] = True
        if " or " in reason and len(call_matches) + len(backtick_matches) >= 2:
            forms["alternative_commands"] = True
            source_forms = [
                reason[match.start() : _balanced_call_end(reason, match.start())]
                for match in call_matches
            ] + [match.group() for match in backtick_matches]
            assert all(source_form in suffix for source_form in source_forms), rule_name

    assert all(forms.values()), forms
