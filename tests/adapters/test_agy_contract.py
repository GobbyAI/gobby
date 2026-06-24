"""AGY hook contract regression tests."""

from __future__ import annotations

import pytest

from gobby.adapters.agy_contract import (
    AGY_EVENT_MAP,
    AGY_HOOK_ALIASES,
    AGY_HOOK_CONTRACTS,
    AGY_HOOK_NAMES,
    get_agy_contract,
)
from gobby.hooks.events import HookEventType

pytestmark = pytest.mark.unit


def test_agy_hook_contract_maps_supported_events() -> None:
    assert AGY_HOOK_NAMES == (
        "PreInvocation",
        "PreToolUse",
        "PostToolUse",
        "PostInvocation",
        "Stop",
    )
    assert AGY_EVENT_MAP == {
        "PreInvocation": HookEventType.BEFORE_AGENT,
        "PreToolUse": HookEventType.BEFORE_TOOL,
        "PostToolUse": HookEventType.AFTER_TOOL,
        "PostInvocation": HookEventType.AFTER_AGENT,
        "Stop": HookEventType.STOP,
    }


def test_agy_pre_tool_contract_blocks_tool_calls() -> None:
    contract = AGY_HOOK_CONTRACTS["PreToolUse"]

    assert contract.blocks_tool_call is True
    assert contract.event_type is HookEventType.BEFORE_TOOL


def test_agy_aliases_resolve_to_contracts() -> None:
    assert AGY_HOOK_ALIASES["before_agent"] == "PreInvocation"
    assert AGY_HOOK_ALIASES["after_agent"] == "PostInvocation"
    assert AGY_HOOK_ALIASES["pre_tool_use"] == "PreToolUse"
    assert AGY_HOOK_ALIASES["post_tool_use"] == "PostToolUse"
    assert AGY_HOOK_ALIASES["stop"] == "Stop"
    assert get_agy_contract("pre_tool_use") is get_agy_contract("PreToolUse")
    assert get_agy_contract("unknown") is None
