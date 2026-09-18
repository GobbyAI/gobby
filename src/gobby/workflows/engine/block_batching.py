"""Per-agent-context, per-response accounting for blocked tool attempts.

A provider that dispatches every tool call of one assistant response in parallel
delivers the whole prepared batch before the agent can read the first denial, so
charging each sibling denial to the consecutive-tool-block budget spends five
attempts on one remediation opportunity. The same payloads name the issuing
agent, and Claude Code subagents share their parent's session id, so one flat
counter also charges one subagent's blocks to another.

This module resolves both identities from the hook payload: the agent context
that issued the call, and whether the CLI closes each response with a tool-batch
boundary event that re-arms counting.
"""

from __future__ import annotations

from typing import Any

from gobby.hooks.events import EVENT_TYPE_CLI_SUPPORT, HookEventType, SessionSource

#: Payload keys naming the concurrent agent context inside one session.
AGENT_CONTEXT_PAYLOAD_KEYS = ("agent_id", "subagent_id")

#: Session variable holding blocked-tool state for non-main agent contexts.
BLOCK_SCOPES_VARIABLE = "_last_blocked_scopes"

#: Scope key marking the current response batch as already counted.
BATCH_COUNTED_KEY = "_last_blocked_batch_counted"

#: Scope key holding the consecutive blocked-attempt count.
BLOCK_COUNT_KEY = "consecutive_tool_blocks"


def agent_context_key(event_data: dict[str, Any]) -> str:
    """Return the payload's concurrent-agent discriminator, empty for the main agent."""
    for key in AGENT_CONTEXT_PAYLOAD_KEYS:
        value = event_data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def block_scope(variables: dict[str, Any], context: str) -> dict[str, Any]:
    """Return the mutable blocked-tool state owned by one agent context.

    The main agent keeps writing the flat session variables so rule conditions
    and step-transition resets keep reading one well-known name. Each concurrent
    subagent gets its own slot under :data:`BLOCK_SCOPES_VARIABLE`.
    """
    if not context:
        return variables
    scopes = variables.get(BLOCK_SCOPES_VARIABLE)
    if not isinstance(scopes, dict):
        scopes = {}
        variables[BLOCK_SCOPES_VARIABLE] = scopes
    scope = scopes.get(context)
    if not isinstance(scope, dict):
        scope = {}
        scopes[context] = scope
    return scope


def clear_block_scopes(variables: dict[str, Any]) -> None:
    """Drop every subagent block scope at a turn boundary."""
    variables[BLOCK_SCOPES_VARIABLE] = {}


def delivers_response_batch_boundary(source: SessionSource | str | None) -> bool:
    """Return whether this CLI closes each assistant response with a tool-batch event."""
    if source is None:
        return False
    value = source.value if isinstance(source, SessionSource) else str(source)
    support = EVENT_TYPE_CLI_SUPPORT.get(HookEventType.POST_TOOL_BATCH, {})
    return bool(support.get(value))


def register_blocked_attempt(
    scope: dict[str, Any],
    *,
    source: SessionSource | str | None,
) -> int:
    """Charge one remediation attempt unless this response's batch already paid.

    Returns the resulting consecutive blocked-attempt count. Without a batch
    boundary event a CLI cannot separate siblings from sequential retries, so
    there every denial counts.
    """
    count = int(scope.get(BLOCK_COUNT_KEY, 0) or 0)
    if scope.get(BATCH_COUNTED_KEY):
        return count
    count += 1
    scope[BLOCK_COUNT_KEY] = count
    if delivers_response_batch_boundary(source):
        scope[BATCH_COUNTED_KEY] = True
    return count


def close_response_batch(scope: dict[str, Any]) -> None:
    """Re-arm attempt counting once the response's tool batch has closed."""
    scope[BATCH_COUNTED_KEY] = False
