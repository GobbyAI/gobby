"""Tests for Droid hook contract metadata."""

import pytest

from gobby.adapters.droid_contract import (
    DROID_EVENT_MAP,
    DROID_HOOK_CONTRACTS,
    DROID_HOOK_EVENT_NAME_MAP,
    DROID_PASCAL_HOOK_NAMES,
    DroidDecisionStyle,
    get_droid_contract,
)
from gobby.hooks.events import HookEventType

pytestmark = pytest.mark.unit


def test_droid_hook_contract_has_exact_plan_events() -> None:
    assert len(DROID_PASCAL_HOOK_NAMES) == 9
    assert len(DROID_HOOK_CONTRACTS) == 9
    assert tuple(DROID_HOOK_CONTRACTS) == DROID_PASCAL_HOOK_NAMES


def test_droid_decision_style_is_standalone() -> None:
    assert DroidDecisionStyle.__module__ == "gobby.adapters.droid_contract"
    assert DroidDecisionStyle.PRE_TOOL_USE.value == "pre_tool_use"


def test_droid_maps_are_derived_from_contract_table() -> None:
    assert DROID_EVENT_MAP == {
        name: contract.event_type for name, contract in DROID_HOOK_CONTRACTS.items()
    }
    assert DROID_HOOK_EVENT_NAME_MAP == {
        name: contract.hook_event_name for name, contract in DROID_HOOK_CONTRACTS.items()
    }


@pytest.mark.parametrize(
    ("hook_name", "event_type"),
    [
        ("PreToolUse", HookEventType.BEFORE_TOOL),
        ("PostToolUse", HookEventType.AFTER_TOOL),
        ("UserPromptSubmit", HookEventType.BEFORE_AGENT),
        ("Notification", HookEventType.NOTIFICATION),
        ("Stop", HookEventType.STOP),
        ("SubagentStop", HookEventType.SUBAGENT_STOP),
        ("PreCompact", HookEventType.PRE_COMPACT),
        ("SessionStart", HookEventType.SESSION_START),
        ("SessionEnd", HookEventType.SESSION_END),
    ],
)
def test_droid_hook_event_type_mapping(
    hook_name: str,
    event_type: HookEventType,
) -> None:
    assert DROID_HOOK_CONTRACTS[hook_name].event_type is event_type


def test_get_droid_contract() -> None:
    contract = get_droid_contract("PreToolUse")
    assert contract is not None
    assert contract.decision_style is DroidDecisionStyle.PRE_TOOL_USE
    assert get_droid_contract(None) is None
    assert get_droid_contract("PermissionRequest") is None
