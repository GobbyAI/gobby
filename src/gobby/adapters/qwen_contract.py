"""Docs-backed Qwen CLI terminal-hook contracts."""

from __future__ import annotations

from dataclasses import dataclass

from gobby.adapters.claude_contract import ClaudeDecisionStyle, ClaudeHookContract
from gobby.hooks.events import HookEventType


@dataclass(frozen=True)
class QwenHookContract(ClaudeHookContract):
    """Qwen event metadata for its Claude-shaped hook protocol."""


QWEN_HOOK_CONTRACTS: tuple[QwenHookContract, ...] = (
    QwenHookContract(
        "SessionStart", "SessionStart", HookEventType.SESSION_START, allows_additional_context=True
    ),
    QwenHookContract("SessionEnd", "SessionEnd", HookEventType.SESSION_END),
    QwenHookContract(
        "UserPromptSubmit",
        "UserPromptSubmit",
        HookEventType.BEFORE_AGENT,
        ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        True,
    ),
    QwenHookContract(
        "PreToolUse",
        "PreToolUse",
        HookEventType.BEFORE_TOOL,
        ClaudeDecisionStyle.PRE_TOOL_USE,
        True,
    ),
    QwenHookContract(
        "PermissionRequest",
        "PermissionRequest",
        HookEventType.PERMISSION_REQUEST,
        ClaudeDecisionStyle.PERMISSION_REQUEST,
    ),
    QwenHookContract(
        "PostToolUse",
        "PostToolUse",
        HookEventType.AFTER_TOOL,
        ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        True,
    ),
    QwenHookContract(
        "PostToolUseFailure",
        "PostToolUseFailure",
        HookEventType.AFTER_TOOL,
        ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        True,
    ),
    QwenHookContract(
        "Stop",
        "Stop",
        HookEventType.STOP,
        ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        True,
    ),
    QwenHookContract("StopFailure", "StopFailure", HookEventType.STOP_FAILURE),
    QwenHookContract(
        "SubagentStart",
        "SubagentStart",
        HookEventType.SUBAGENT_START,
        allows_additional_context=True,
    ),
    QwenHookContract(
        "SubagentStop",
        "SubagentStop",
        HookEventType.SUBAGENT_STOP,
        ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
    ),
    QwenHookContract(
        "PreCompact",
        "PreCompact",
        HookEventType.PRE_COMPACT,
        ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        True,
    ),
    QwenHookContract(
        "PostCompact",
        "PostCompact",
        HookEventType.POST_COMPACT,
        allows_additional_context=True,
    ),
    QwenHookContract(
        "Notification",
        "Notification",
        HookEventType.NOTIFICATION,
        allows_additional_context=True,
    ),
    QwenHookContract(
        "TodoCreated",
        "TodoCreated",
        HookEventType.TASK_CREATED,
        ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
    ),
    QwenHookContract(
        "TodoCompleted",
        "TodoCompleted",
        HookEventType.TASK_COMPLETED,
        ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
    ),
)

QWEN_HOOK_CONTRACTS_BY_NAME = {contract.native_name: contract for contract in QWEN_HOOK_CONTRACTS}
QWEN_HOOK_NAMES = tuple(contract.native_name for contract in QWEN_HOOK_CONTRACTS)
QWEN_EVENT_MAP = {contract.native_name: contract.event_type for contract in QWEN_HOOK_CONTRACTS}
QWEN_HOOK_EVENT_NAME_MAP = {
    contract.native_name: contract.hook_event_name for contract in QWEN_HOOK_CONTRACTS
}


def get_qwen_contract(hook_type: str | None) -> QwenHookContract | None:
    """Return the contract for an exact current Qwen hook name."""

    if not hook_type:
        return None
    return QWEN_HOOK_CONTRACTS_BY_NAME.get(hook_type)
