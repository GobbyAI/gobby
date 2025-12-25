"""Context injection utilities for Claude Code hooks.

This module provides reusable functions for injecting session context and restored
summaries into Claude Code sessions via hook responses.

Supports:
- Building session metadata context
- Building context restoration messages
- Creating hook responses with systemMessage and additionalContext fields
"""

from typing import Any


def build_session_context(
    session_id: str,
    user_id: str,
    machine_id: str,
    parent_session_id: str | None = None,
) -> str:
    """Build session metadata context for additionalContext field.

    Args:
        session_id: Database session ID (sessions.id UUID)
        user_id: User UUID
        machine_id: Machine identifier
        parent_session_id: Optional parent session ID if this is a handoff

    Returns:
        Formatted markdown string with session metadata
    """
    context = f"""## Gobby Session Context
- Session ID: `{session_id}`
- User ID: `{user_id}`
- Machine ID: `{machine_id}`"""

    if parent_session_id:
        context += f"\n- Parent Session: `{parent_session_id}` (handoff completed)"

    return context


def build_restored_context(
    session_id: str,
    parent_session_id: str,
    external_id: str,
    summary_markdown: str,
) -> dict[str, str]:
    """Build context restoration message for session handoff.

    Args:
        session_id: New session's database ID
        parent_session_id: Previous session's database ID
        external_id: Claude Code session identifier
        summary_markdown: Restored session summary

    Returns:
        Dict with 'system_message' (user-visible) and 'additional_context' (AI-only)
    """
    system_message = f"""⏺ Context restored from the previous session.
  Session ID: {session_id}
  Parent ID: {parent_session_id}
  Claude Code ID: {external_id}"""

    additional_context = f"""## Previous Session Context

{summary_markdown}"""

    return {
        "system_message": system_message,
        "additional_context": additional_context,
    }


def inject_context_into_response(
    response: dict[str, Any],
    session_id: str,
    user_id: str,
    machine_id: str,
    parent_session_id: str | None = None,
    restored_summary: str | None = None,
    external_id: str | None = None,
) -> dict[str, Any]:
    """Inject session context into a hook response.

    This function modifies the response dict to include:
    - additionalContext: Session metadata + optional restored summary (AI-only)
    - systemMessage: User-visible message for handoff scenarios (optional)

    Args:
        response: Hook response dict to modify
        session_id: Database session ID
        user_id: User UUID
        machine_id: Machine identifier
        parent_session_id: Optional parent session ID if this is a handoff
        restored_summary: Optional restored session summary markdown
        external_id: Optional Claude Code session ID (required if restored_summary provided)

    Returns:
        Modified response dict with context injected
    """
    # Build base session context
    session_context = build_session_context(
        session_id=session_id,
        user_id=user_id,
        machine_id=machine_id,
        parent_session_id=parent_session_id,
    )

    # If we have restored context, add it
    if restored_summary and external_id and parent_session_id:
        restored = build_restored_context(
            session_id=session_id,
            parent_session_id=parent_session_id,
            external_id=external_id,
            summary_markdown=restored_summary,
        )

        # Combine session context with restored context
        full_context = f"{session_context}\n\n---\n\n{restored['additional_context']}"

        # Add user-visible system message for handoff
        response["systemMessage"] = restored["system_message"]
    else:
        # No restoration, just session metadata
        full_context = session_context

    # Set additionalContext in hookSpecificOutput
    if "hookSpecificOutput" not in response:
        response["hookSpecificOutput"] = {}

    response["hookSpecificOutput"]["additionalContext"] = full_context

    return response
