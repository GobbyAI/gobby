"""Formatting helpers for session summaries and handoff context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.hooks.tool_error_tracker import normalize_open_tool_error_records

if TYPE_CHECKING:
    from gobby.sessions.analyzer import HandoffContext

_ERROR_PREVIEW_CHARS = 300
_OPEN_TOOL_ERRORS_RETRIEVAL = (
    'get_variable(name="open_tool_errors", session_id=<current>), then select the record by '
    'error_id="{error_id}"'
)


def _stored_transcript_preview(content: str, preview_chars: int) -> str:
    """Render a bounded transcript head when content is longer."""
    if len(content) <= preview_chars:
        return content
    return f"{content[:preview_chars]}\n... [truncated]"


def format_unresolved_errors(records: list[dict[str, Any]]) -> str:
    """Render bounded unresolved-error previews with exact full-payload retrieval."""
    if not records:
        return ""
    lines: list[str] = []
    for raw_record in records:
        normalized = normalize_open_tool_error_records([raw_record])
        if not normalized:
            continue
        record = normalized[0]
        preview = record["error"][:_ERROR_PREVIEW_CHARS]
        retrieval = _OPEN_TOOL_ERRORS_RETRIEVAL.format(error_id=record["error_id"])
        lines.append(
            f"- error_id: {record['error_id']} | tool: {record['tool']} | "
            f"target: {record['target_key']} | error preview: {preview} | "
            f"full error: {retrieval} | count: {record['count']}"
        )
    if not lines:
        return ""
    return "Unresolved Tool Errors:\n" + "\n".join(lines)


def _get_result_truncation_limit(content_str: str) -> int:
    """Return truncation limit based on content type.

    Errors/test output get 1000 chars for visibility. Default: 200 chars.
    """
    error_indicators = [
        "Error",
        "error",
        "ERROR",
        "Failed",
        "failed",
        "Traceback",
        "Exception",
        "FAIL",
        "AssertionError",
    ]
    if any(ind in content_str[:500] for ind in error_indicators):
        return 1000
    test_indicators = ["pytest", "PASSED", "FAILED", "test_", "npm test"]
    if any(ind in content_str[:500] for ind in test_indicators):
        return 1000
    return 200


def format_turns_for_llm(turns: list[dict[str, Any]]) -> str:
    """Format transcript turns for LLM analysis.

    Handles both Claude Code format (nested message.role/content) and typed
    JSON format (flat type/role/content).

    Args:
        turns: List of transcript turn dicts

    Returns:
        Formatted string with turn summaries
    """
    formatted: list[str] = []
    for i, turn in enumerate(turns):
        # Detect format: typed JSON uses "type" field, Claude uses nested "message"
        event_type = turn.get("type")

        if event_type:
            # Typed JSON format: flat structure with type field
            role, content = _format_typed_json_turn(turn, event_type)
            if role is None:
                continue  # Skip non-displayable events
        else:
            # Claude Code format: nested message structure
            role, content = _format_claude_turn(turn)

        formatted.append(f"[Turn {i + 1} - {role}]: {content}")

    return "\n\n".join(formatted)


def _format_typed_json_turn(
    turn: dict[str, Any],
    event_type: str,
) -> tuple[str | None, str]:
    """Format a typed-JSON transcript turn.

    Returns:
        Tuple of (role, formatted_content) or (None, "") if should skip
    """
    if event_type == "message":
        role = turn.get("role", "unknown")
        if role == "model":
            role = "assistant"
        content = turn.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(part) for part in content)
        return role, str(content)

    elif event_type == "tool_use":
        tool_name = turn.get("tool_name") or turn.get("function_name", "unknown")
        params = turn.get("parameters") or turn.get("args", {})
        param_preview = _stored_transcript_preview(str(params), 100) if params else ""
        return "assistant", f"[Tool: {tool_name}] {param_preview}"

    elif event_type == "tool_result":
        tool_name = turn.get("tool_name", "")
        output = turn.get("output") or turn.get("result", "")
        output_str = str(output)
        limit = _get_result_truncation_limit(output_str)
        preview = _stored_transcript_preview(output_str, limit)
        return "tool", f"[Result{' from ' + tool_name if tool_name else ''}]: {preview}"

    elif event_type in ("init", "result"):
        # Skip initialization and final result events
        return None, ""

    else:
        # Unknown type, try to extract something
        content = turn.get("content", turn.get("message", ""))
        return "unknown", _stored_transcript_preview(str(content), 200)


def _format_claude_turn(turn: dict[str, Any]) -> tuple[str, str]:
    """Format a Claude Code turn with nested message structure."""
    message = turn.get("message", {})
    role = message.get("role", "unknown")
    content = message.get("content", "")

    # Assistant messages have content as array of blocks
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "thinking":
                    text_parts.append(f"[Thinking: {block.get('thinking', '')}]")
                elif block.get("type") == "tool_use":
                    text_parts.append(f"[Tool: {block.get('name', 'unknown')}]")
                elif block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    # Extract text from list of content blocks if needed
                    if isinstance(result_content, list):
                        extracted = []
                        for item in result_content:
                            if isinstance(item, dict):
                                extracted.append(item.get("text", "") or item.get("content", ""))
                            else:
                                extracted.append(str(item))
                        result_content = " ".join(extracted)
                    content_str = str(result_content)
                    limit = _get_result_truncation_limit(content_str)
                    preview = _stored_transcript_preview(content_str, limit)
                    text_parts.append(f"[Result: {preview}]")
        content = " ".join(text_parts)

    return role, str(content)


def _format_structured_context(ctx: HandoffContext) -> str:
    """Format HandoffContext fields as concise text for LLM consumption.

    Args:
        ctx: Structured context extracted from transcript analysis

    Returns:
        Formatted text block with anchoring data (files, commits, decisions)
    """
    sections: list[str] = []

    if ctx.active_gobby_task:
        task = ctx.active_gobby_task
        if isinstance(task, dict):
            sections.append(
                f"Active Task: {task.get('title', 'Untitled')} "
                f"(#{task.get('id', '?')}, status: {task.get('status', 'unknown')})"
            )
        else:
            sections.append(f"Active Task: {task}")

    if ctx.task_progress:
        progress_lines = []
        for p in ctx.task_progress[-15:]:
            if isinstance(p, dict):
                progress_lines.append(
                    f"  - {p.get('action', '?')}: {p.get('title', '?')} ({p.get('id', '?')})"
                )
            else:
                progress_lines.append(f"  - {p}")
        sections.append("Task Progress:\n" + "\n".join(progress_lines))

    if ctx.initial_goal:
        sections.append(f"Original Goal: {_stored_transcript_preview(ctx.initial_goal, 500)}")

    if ctx.unresolved_errors:
        sections.append(format_unresolved_errors(ctx.unresolved_errors))

    if ctx.files_modified:
        sections.append("Files Modified:\n" + "\n".join(f"  - {f}" for f in ctx.files_modified))

    if ctx.git_commits:
        commit_lines = []
        for c in ctx.git_commits[:10]:
            if isinstance(c, dict):
                commit_lines.append(f"  - {c.get('hash', '')[:7]} {c.get('message', '')}")
            else:
                commit_lines.append(f"  - {c}")
        sections.append("Recent Commits:\n" + "\n".join(commit_lines))

    if ctx.recent_activity:
        sections.append(
            "Recent Activity:\n" + "\n".join(f"  - {a}" for a in ctx.recent_activity[-10:])
        )

    if ctx.key_decisions:
        sections.append("Key Decisions:\n" + "\n".join(f"  - {d}" for d in ctx.key_decisions))

    return "\n\n".join(sections) if sections else ""
