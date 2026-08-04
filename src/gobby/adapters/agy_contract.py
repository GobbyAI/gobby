"""Shared AGY hook contract data."""

from __future__ import annotations

from dataclasses import dataclass

from gobby.hooks.events import HookEventType


@dataclass(frozen=True)
class AgyHookContract:
    """Per-event metadata driving AGY hook translation."""

    hook_event_name: str
    event_type: HookEventType
    blocks_tool_call: bool = False


AGY_HOOK_NAMES: tuple[str, ...] = (
    "PreInvocation",
    "PreToolUse",
    "PostToolUse",
    "PostInvocation",
    "Stop",
)

# AGY keys hooks.json by hook *name*, not by the literal "hooks". Everything
# Gobby owns lives under this one named hook so third-party names survive
# install and uninstall untouched.
AGY_GOBBY_HOOK_NAME = "gobby"

# AGY groups tool events behind a matcher/hooks wrapper and takes the remaining
# lifecycle events as flat handler lists. Writing a flat event in the grouped
# shape makes AGY reject the whole file with "command hook must specify
# 'command'", which disables every hook in it.
AGY_GROUPED_HOOK_NAMES: tuple[str, ...] = ("PreToolUse", "PostToolUse")
AGY_FLAT_HOOK_NAMES: tuple[str, ...] = tuple(
    name for name in AGY_HOOK_NAMES if name not in AGY_GROUPED_HOOK_NAMES
)

# AGY documents `timeout` in seconds.
AGY_HOOK_TIMEOUT_SECONDS = 45


AGY_HOOK_CONTRACTS: dict[str, AgyHookContract] = {
    "PreInvocation": AgyHookContract(
        hook_event_name="PreInvocation",
        event_type=HookEventType.BEFORE_AGENT,
    ),
    "PreToolUse": AgyHookContract(
        hook_event_name="PreToolUse",
        event_type=HookEventType.BEFORE_TOOL,
        blocks_tool_call=True,
    ),
    "PostToolUse": AgyHookContract(
        hook_event_name="PostToolUse",
        event_type=HookEventType.AFTER_TOOL,
    ),
    "PostInvocation": AgyHookContract(
        hook_event_name="PostInvocation",
        event_type=HookEventType.AFTER_AGENT,
    ),
    "Stop": AgyHookContract(
        hook_event_name="Stop",
        event_type=HookEventType.STOP,
    ),
}


AGY_EVENT_MAP: dict[str, HookEventType] = {
    name: contract.event_type for name, contract in AGY_HOOK_CONTRACTS.items()
}


AGY_HOOK_ALIASES: dict[str, str] = {
    "before_agent": "PreInvocation",
    "after_agent": "PostInvocation",
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "stop": "Stop",
}


def get_agy_contract(hook_type: str | None) -> AgyHookContract | None:
    """Resolve an AGY hook name or alias to its contract."""

    if not hook_type:
        return None
    return AGY_HOOK_CONTRACTS.get(hook_type) or AGY_HOOK_CONTRACTS.get(
        AGY_HOOK_ALIASES.get(hook_type, "")
    )
