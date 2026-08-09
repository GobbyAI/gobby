"""Shared Claude hook contract data.

This module is the single source of truth for Claude hook names, unified event
mapping, and docs-backed response-shape policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from gobby.hooks.events import HookEventType, HookResponse


class ClaudeDecisionStyle(str, Enum):
    """How a Claude hook event expresses response control."""

    NONE = "none"
    TOP_LEVEL_BLOCK = "top_level_block"
    PRE_TOOL_USE = "pre_tool_use"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DENIED = "permission_denied"
    HARD_STOP = "hard_stop"
    WATCH_PATHS = "watch_paths"
    WORKTREE_CREATE = "worktree_create"
    ELICITATION = "elicitation"
    ELICITATION_RESULT = "elicitation_result"
    IGNORE_BLOCK = "ignore_block"
    DISPLAY_CONTENT = "display_content"


@dataclass(frozen=True)
class ClaudeHookContract:
    """Docs-backed metadata for a Claude hook event."""

    native_name: str
    hook_event_name: str
    event_type: HookEventType
    decision_style: ClaudeDecisionStyle = ClaudeDecisionStyle.NONE
    allows_additional_context: bool = False


CLAUDE_HOOK_CONTRACTS: tuple[ClaudeHookContract, ...] = (
    ClaudeHookContract(
        native_name="session-start",
        hook_event_name="SessionStart",
        event_type=HookEventType.SESSION_START,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="setup",
        hook_event_name="Setup",
        event_type=HookEventType.SETUP,
        decision_style=ClaudeDecisionStyle.IGNORE_BLOCK,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="instructions-loaded",
        hook_event_name="InstructionsLoaded",
        event_type=HookEventType.INSTRUCTIONS_LOADED,
    ),
    ClaudeHookContract(
        native_name="user-prompt-submit",
        hook_event_name="UserPromptSubmit",
        event_type=HookEventType.BEFORE_AGENT,
        decision_style=ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="user-prompt-expansion",
        hook_event_name="UserPromptExpansion",
        event_type=HookEventType.USER_PROMPT_EXPANSION,
        decision_style=ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="pre-tool-use",
        hook_event_name="PreToolUse",
        event_type=HookEventType.BEFORE_TOOL,
        decision_style=ClaudeDecisionStyle.PRE_TOOL_USE,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="permission-request",
        hook_event_name="PermissionRequest",
        event_type=HookEventType.PERMISSION_REQUEST,
        decision_style=ClaudeDecisionStyle.PERMISSION_REQUEST,
    ),
    ClaudeHookContract(
        native_name="post-tool-use",
        hook_event_name="PostToolUse",
        event_type=HookEventType.AFTER_TOOL,
        decision_style=ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="post-tool-use-failure",
        hook_event_name="PostToolUseFailure",
        event_type=HookEventType.AFTER_TOOL,
        decision_style=ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="post-tool-batch",
        hook_event_name="PostToolBatch",
        event_type=HookEventType.POST_TOOL_BATCH,
        decision_style=ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="permission-denied",
        hook_event_name="PermissionDenied",
        event_type=HookEventType.PERMISSION_DENIED,
        decision_style=ClaudeDecisionStyle.PERMISSION_DENIED,
    ),
    ClaudeHookContract(
        native_name="notification",
        hook_event_name="Notification",
        event_type=HookEventType.NOTIFICATION,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="message-display",
        hook_event_name="MessageDisplay",
        event_type=HookEventType.MESSAGE_DISPLAY,
        decision_style=ClaudeDecisionStyle.DISPLAY_CONTENT,
    ),
    ClaudeHookContract(
        native_name="subagent-start",
        hook_event_name="SubagentStart",
        event_type=HookEventType.SUBAGENT_START,
        allows_additional_context=True,
    ),
    ClaudeHookContract(
        native_name="subagent-stop",
        hook_event_name="SubagentStop",
        event_type=HookEventType.SUBAGENT_STOP,
        decision_style=ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
    ),
    ClaudeHookContract(
        native_name="task-created",
        hook_event_name="TaskCreated",
        event_type=HookEventType.TASK_CREATED,
        decision_style=ClaudeDecisionStyle.HARD_STOP,
    ),
    ClaudeHookContract(
        native_name="task-completed",
        hook_event_name="TaskCompleted",
        event_type=HookEventType.TASK_COMPLETED,
        decision_style=ClaudeDecisionStyle.HARD_STOP,
    ),
    ClaudeHookContract(
        native_name="stop",
        hook_event_name="Stop",
        event_type=HookEventType.STOP,
        decision_style=ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
    ),
    ClaudeHookContract(
        native_name="stop-failure",
        hook_event_name="StopFailure",
        event_type=HookEventType.STOP_FAILURE,
    ),
    ClaudeHookContract(
        native_name="teammate-idle",
        hook_event_name="TeammateIdle",
        event_type=HookEventType.TEAMMATE_IDLE,
        decision_style=ClaudeDecisionStyle.HARD_STOP,
    ),
    ClaudeHookContract(
        native_name="config-change",
        hook_event_name="ConfigChange",
        event_type=HookEventType.CONFIG_CHANGE,
        decision_style=ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
    ),
    ClaudeHookContract(
        native_name="cwd-changed",
        hook_event_name="CwdChanged",
        event_type=HookEventType.CWD_CHANGED,
        decision_style=ClaudeDecisionStyle.WATCH_PATHS,
    ),
    ClaudeHookContract(
        native_name="directory-added",
        hook_event_name="DirectoryAdded",
        event_type=HookEventType.DIRECTORY_ADDED,
        decision_style=ClaudeDecisionStyle.IGNORE_BLOCK,
    ),
    ClaudeHookContract(
        native_name="file-changed",
        hook_event_name="FileChanged",
        event_type=HookEventType.FILE_CHANGED,
        decision_style=ClaudeDecisionStyle.WATCH_PATHS,
    ),
    ClaudeHookContract(
        native_name="worktree-create",
        hook_event_name="WorktreeCreate",
        event_type=HookEventType.WORKTREE_CREATE,
        decision_style=ClaudeDecisionStyle.WORKTREE_CREATE,
    ),
    ClaudeHookContract(
        native_name="worktree-remove",
        hook_event_name="WorktreeRemove",
        event_type=HookEventType.WORKTREE_REMOVE,
    ),
    ClaudeHookContract(
        native_name="pre-compact",
        hook_event_name="PreCompact",
        event_type=HookEventType.PRE_COMPACT,
        decision_style=ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
    ),
    ClaudeHookContract(
        native_name="post-compact",
        hook_event_name="PostCompact",
        event_type=HookEventType.POST_COMPACT,
    ),
    ClaudeHookContract(
        native_name="session-end",
        hook_event_name="SessionEnd",
        event_type=HookEventType.SESSION_END,
    ),
    ClaudeHookContract(
        native_name="elicitation",
        hook_event_name="Elicitation",
        event_type=HookEventType.ELICITATION,
        decision_style=ClaudeDecisionStyle.ELICITATION,
    ),
    ClaudeHookContract(
        native_name="elicitation-result",
        hook_event_name="ElicitationResult",
        event_type=HookEventType.ELICITATION_RESULT,
        decision_style=ClaudeDecisionStyle.ELICITATION_RESULT,
    ),
)

CLAUDE_HOOK_CONTRACTS_BY_NATIVE: dict[str, ClaudeHookContract] = {
    contract.native_name: contract for contract in CLAUDE_HOOK_CONTRACTS
}
CLAUDE_HOOK_CONTRACTS_BY_EVENT_NAME: dict[str, ClaudeHookContract] = {
    contract.hook_event_name: contract for contract in CLAUDE_HOOK_CONTRACTS
}

CLAUDE_EVENT_MAP: dict[str, HookEventType] = {
    contract.native_name: contract.event_type for contract in CLAUDE_HOOK_CONTRACTS
}
CLAUDE_HOOK_EVENT_NAME_MAP: dict[str, str] = {
    contract.native_name: contract.hook_event_name for contract in CLAUDE_HOOK_CONTRACTS
}
CLAUDE_PASCAL_HOOK_NAMES: tuple[str, ...] = tuple(
    contract.hook_event_name for contract in CLAUDE_HOOK_CONTRACTS
)
CLAUDE_NATIVE_HOOK_NAMES: tuple[str, ...] = tuple(
    contract.native_name for contract in CLAUDE_HOOK_CONTRACTS
)
CLAUDE_ADDITIONAL_CONTEXT_EVENT_NAMES: frozenset[str] = frozenset(
    contract.hook_event_name
    for contract in CLAUDE_HOOK_CONTRACTS
    if contract.allows_additional_context
)


def get_claude_contract(hook_type: str | None) -> ClaudeHookContract | None:
    """Look up the contract for a Claude hook name.

    Accepts either the native kebab name (``user-prompt-submit``) or the
    PascalCase hook event name (``UserPromptSubmit``). Claude Code requires
    PascalCase keys in ``settings.json``; tolerating both here means an installed
    config that passes the PascalCase token through to ``--type`` still resolves
    to the correct contract instead of silently degrading to ``NOTIFICATION``.
    """

    if not hook_type:
        return None
    contract = CLAUDE_HOOK_CONTRACTS_BY_NATIVE.get(hook_type)
    if contract is not None:
        return contract
    return CLAUDE_HOOK_CONTRACTS_BY_EVENT_NAME.get(hook_type)


def build_graceful_error_hook_response(error_msg: str) -> HookResponse:
    """Return a non-fatal hook response that explains the daemon error."""

    return HookResponse(
        decision="allow",
        context=(
            f"Gobby hook error (non-fatal): {error_msg}. Tool execution will proceed normally."
        ),
    )
