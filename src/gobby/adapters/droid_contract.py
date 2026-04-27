"""Shared Droid hook contract data.

This module is the single source of truth for Factory Droid hook names, unified
event mapping, and response-shape policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from gobby.hooks.events import HookEventType


class DroidDecisionStyle(str, Enum):
    """How a Droid hook event expresses response control."""

    TOP_LEVEL_BLOCK = "top_level_block"
    PRE_TOOL_USE = "pre_tool_use"
    NONE = "none"


@dataclass(frozen=True)
class DroidHookContract:
    """Per-event metadata driving Droid response translation."""

    hook_event_name: str
    event_type: HookEventType
    decision_style: DroidDecisionStyle
    allows_additional_context: bool


DROID_PASCAL_HOOK_NAMES: tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Notification",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionStart",
    "SessionEnd",
)


DROID_HOOK_CONTRACTS: dict[str, DroidHookContract] = {
    "PreToolUse": DroidHookContract(
        hook_event_name="PreToolUse",
        event_type=HookEventType.BEFORE_TOOL,
        decision_style=DroidDecisionStyle.PRE_TOOL_USE,
        allows_additional_context=False,
    ),
    "PostToolUse": DroidHookContract(
        hook_event_name="PostToolUse",
        event_type=HookEventType.AFTER_TOOL,
        decision_style=DroidDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=True,
    ),
    "UserPromptSubmit": DroidHookContract(
        hook_event_name="UserPromptSubmit",
        event_type=HookEventType.BEFORE_AGENT,
        decision_style=DroidDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=True,
    ),
    "Notification": DroidHookContract(
        hook_event_name="Notification",
        event_type=HookEventType.NOTIFICATION,
        decision_style=DroidDecisionStyle.NONE,
        allows_additional_context=False,
    ),
    "Stop": DroidHookContract(
        hook_event_name="Stop",
        event_type=HookEventType.STOP,
        decision_style=DroidDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=False,
    ),
    "SubagentStop": DroidHookContract(
        hook_event_name="SubagentStop",
        event_type=HookEventType.SUBAGENT_STOP,
        decision_style=DroidDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=False,
    ),
    "PreCompact": DroidHookContract(
        hook_event_name="PreCompact",
        event_type=HookEventType.PRE_COMPACT,
        decision_style=DroidDecisionStyle.NONE,
        allows_additional_context=False,
    ),
    "SessionStart": DroidHookContract(
        hook_event_name="SessionStart",
        event_type=HookEventType.SESSION_START,
        decision_style=DroidDecisionStyle.NONE,
        allows_additional_context=True,
    ),
    "SessionEnd": DroidHookContract(
        hook_event_name="SessionEnd",
        event_type=HookEventType.SESSION_END,
        decision_style=DroidDecisionStyle.NONE,
        allows_additional_context=False,
    ),
}


DROID_EVENT_MAP: dict[str, HookEventType] = {
    name: contract.event_type for name, contract in DROID_HOOK_CONTRACTS.items()
}

DROID_HOOK_EVENT_NAME_MAP: dict[str, str] = {
    name: contract.hook_event_name for name, contract in DROID_HOOK_CONTRACTS.items()
}


def get_droid_contract(hook_type: str | None) -> DroidHookContract | None:
    """Resolve a PascalCase Droid hook name to its contract."""

    if not hook_type:
        return None
    return DROID_HOOK_CONTRACTS.get(hook_type)
