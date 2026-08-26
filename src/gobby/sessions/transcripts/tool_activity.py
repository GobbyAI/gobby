"""Bounded, transcript-derived tool activity ledgers."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gobby.adapters.acp_tool_names import normalize_acp_tool_name
from gobby.adapters.codex_impl.execution_chain import extract_functions_exec_command
from gobby.hooks._normalization_shell import canonicalize_shell_tool_name
from gobby.mcp_proxy._call_tool_wrapper import (
    CallToolWrapperInputError,
    canonicalize_call_tool_wrapper,
)
from gobby.sessions.transcripts.codex_items import (
    mcp_item_failure,
    normalize_command_execution,
)

DIGEST_ACTIVITY_MAX_LINES = 80
DIGEST_ACTIVITY_MAX_CHARS = 6000
DIGEST_ACTIVITY_TAIL_LINES = 10
ACTIVITY_HEADER = "[tool activity]"

_LEDGER_SHELL_ALIASES = {"run_terminal_command": "Bash"}
_CALL_TOOL_WRAPPERS = {
    "call_tool",
    "mcp__gobby__call_tool",
    "gobby__call_tool",
    "mcp_call_tool",
}
_TASK_MUTATIONS = {"claim_task", "close_task", "update_task", "link_commit", "create_task"}
_EDIT_TOOLS = {"apply_patch", "Edit", "Write", "search_replace", "write_file"}
_PATH_KEYS = ("file_path", "target_file", "path", "notebook_path", "TargetFile")
_COMMIT_LINE = re.compile(r"^\[[^\]]+\s+([0-9a-fA-F]{7,40})\]\s*(.*)$", re.MULTILINE)


@dataclass
class ToolActivityEntry:
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str | None = None
    error: str | None = None
    outcome: str | None = None
    resolved: bool = False
    record_index: int = -1


@dataclass(frozen=True)
class _CollapsedLine:
    line: str
    entries: tuple[ToolActivityEntry, ...]

    @property
    def count(self) -> int:
        return len(self.entries)


def fresh_scan_parser(parser: Any) -> Any:
    """Create an unhydrated parser for observational event scans."""
    return type(parser)(session_id=parser.session_id)


def claude_activity_by_user_index(turns: list[dict[str, Any]]) -> dict[int, str]:
    """Collect Claude content-block activity under its opening user text record."""
    entries_by_user: dict[int, list[ToolActivityEntry]] = {}
    entries_by_id: dict[str, ToolActivityEntry] = {}
    current_user: int | None = None
    for index, turn in enumerate(turns):
        message = turn.get("message")
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "user" and _content_has_text(content):
            current_user = index
            entries_by_user.setdefault(index, [])
        if not isinstance(content, list) or current_user is None:
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if role == "assistant" and block_type == "tool_use":
                name, tool_input = canonical_tool_name(block.get("name"), block.get("input"))
                tool_use_id = _string_value(block.get("id"))
                entry = ToolActivityEntry(name, tool_input, tool_use_id=tool_use_id)
                entries_by_user[current_user].append(entry)
                if tool_use_id is not None:
                    entries_by_id[tool_use_id] = entry
            elif role == "user" and block_type == "tool_result":
                tool_use_id = _string_value(block.get("tool_use_id"))
                resolved_entry = entries_by_id.get(tool_use_id or "")
                if resolved_entry is None:
                    continue
                resolved_entry.resolved = True
                result_text = _content_text(block.get("content"))
                if block.get("is_error") is True:
                    resolved_entry.error = result_text
                elif is_commit_producing(resolved_entry.tool_name, resolved_entry.tool_input):
                    resolved_entry.outcome = commit_outcome(
                        resolved_entry.tool_name, resolved_entry.tool_input, result_text
                    )
    return {
        index: render_tool_activity(entries)
        for index, entries in entries_by_user.items()
        if entries
    }


def codex_item_activity(turns: list[dict[str, Any]]) -> list[ToolActivityEntry] | None:
    """Project positioned Codex completed tool items without changing parser events."""
    entries: list[ToolActivityEntry] = []
    saw_tool_item = False
    for record_index, record in enumerate(turns):
        payload = record.get("payload")
        if not isinstance(payload, Mapping) or payload.get("type") != "item_completed":
            continue
        item = payload.get("item")
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == "McpToolCall":
            saw_tool_item = True
            if item.get("server") == "gobby" and item.get("tool") == "call_tool":
                wrapper_input = item.get("arguments")
            else:
                wrapper_input = {
                    "server_name": item.get("server"),
                    "tool_name": item.get("tool"),
                    "arguments": item.get("arguments"),
                }
            name, tool_input = canonical_tool_name("mcp_call_tool", wrapper_input)
            error = mcp_item_failure(item)
            entry = ToolActivityEntry(
                name,
                tool_input,
                error=error,
                resolved=True,
                record_index=record_index,
            )
            if error is None and is_commit_producing(name, tool_input):
                entry.outcome = commit_outcome(name, tool_input, None)
            entries.append(entry)
        elif item_type == "CommandExecution":
            saw_tool_item = True
            outcome = normalize_command_execution(item)
            if outcome is None:
                continue
            error = None
            if outcome.success is False or item.get("status") == "failed":
                error = outcome.output or "command failed"
            entry = ToolActivityEntry(
                "Bash",
                {"command": outcome.command},
                error=error,
                resolved=outcome.success is not None,
                record_index=record_index,
            )
            if outcome.success is True and is_commit_producing(entry.tool_name, entry.tool_input):
                entry.outcome = commit_outcome(entry.tool_name, entry.tool_input, outcome.output)
            entries.append(entry)
        elif item_type == "FileChange":
            saw_tool_item = True
            changes = item.get("changes")
            if not isinstance(changes, Mapping):
                continue
            error = "failed" if item.get("status") == "failed" else None
            for path in changes:
                entries.append(
                    ToolActivityEntry(
                        "apply_patch",
                        {"file_path": str(path)},
                        error=error,
                        resolved=True,
                        record_index=record_index,
                    )
                )
    return entries if saw_tool_item else None


def event_activity_by_user_index(parser: Any, turns: list[dict[str, Any]]) -> dict[int, str]:
    """Collect normalized parser events without mutating the handed parser."""
    from gobby.sessions.transcripts.base import ParsedMessage, raw_lines_from_texts

    scan = fresh_scan_parser(parser)
    wrappers: list[ToolActivityEntry] = []
    by_id: dict[str, ToolActivityEntry] = {}
    texts = [json.dumps(turn, default=str) for turn in turns]
    for event in scan.iter_parse_events(raw_lines_from_texts(texts)):
        for record in event.records:
            if not isinstance(record, ParsedMessage):
                continue
            if record.content_type == "tool_use":
                name, tool_input = canonical_tool_name(record.tool_name, record.tool_input)
                command = pending_exec_command(name, tool_input)
                if command is not None:
                    name, tool_input = "Bash", {"command": command}
                entry = ToolActivityEntry(
                    name,
                    tool_input,
                    tool_use_id=record.tool_use_id,
                    record_index=event.raw_line_no,
                )
                wrappers.append(entry)
                if record.tool_use_id:
                    by_id[record.tool_use_id] = entry
            elif record.content_type == "tool_result":
                resolved_entry = by_id.get(record.tool_use_id or "")
                if resolved_entry is None:
                    continue
                _resolve_entry(resolved_entry, record.tool_result, record.content)
        for outcome in event.codex_exec_outcomes:
            result = outcome.result
            outer = by_id.get(outcome.outer_call_id)
            if outer is None or outer.tool_name != "Bash":
                outer = ToolActivityEntry(
                    "Bash",
                    {"command": outcome.command},
                    tool_use_id=outcome.outer_call_id,
                    record_index=event.raw_line_no,
                )
                wrappers.append(outer)
            else:
                outer.tool_input = {"command": outcome.command}
            _resolve_entry(outer, result, result.get("output"))

    user_indexes = [
        index for index, turn in enumerate(turns) if _is_user_text_record(parser.cli_name, turn)
    ]
    if not user_indexes:
        return {}
    item_entries = codex_item_activity(turns) if parser.cli_name == "codex" else None
    wrapper_buckets = _partition_entries(wrappers, user_indexes)
    item_buckets = _partition_entries(item_entries or [], user_indexes)
    rendered: dict[int, str] = {}
    for user_index in user_indexes:
        bucket = wrapper_buckets.get(user_index, [])
        items = item_buckets.get(user_index, [])
        if item_entries is not None:
            bucket = _suppress_item_wrappers(items, bucket)
        combined = sorted([*items, *bucket], key=lambda entry: entry.record_index)
        if combined:
            rendered[user_index] = render_tool_activity(combined)
    return rendered


def canonical_tool_name(tool_name: str | None, tool_input: Any) -> tuple[str, dict[str, Any]]:
    """Unwrap dispatchers and normalize tool aliases without trusting parser output."""
    if not isinstance(tool_name, str):
        return "unknown-tool", {}
    normalized_input = dict(tool_input) if isinstance(tool_input, Mapping) else {}
    if tool_name in _CALL_TOOL_WRAPPERS:
        try:
            wrapper = canonicalize_call_tool_wrapper(
                server_name=_string_value(normalized_input.get("server_name")),
                tool_name=_string_value(normalized_input.get("tool_name")),
                arguments=normalized_input.get("arguments"),
                args=normalized_input.get("args"),
                session_id=_string_value(normalized_input.get("session_id")),
                project_id=_string_value(normalized_input.get("project_id")),
                intent=_string_value(normalized_input.get("intent")),
            )
        except CallToolWrapperInputError:
            return tool_name, {}
        if not wrapper.server_name or not wrapper.tool_name:
            return tool_name, {}
        arguments = _mapping_value(wrapper.arguments)
        return f"mcp {wrapper.server_name}:{wrapper.tool_name}", arguments

    if tool_name == "use_tool":
        nested_name = normalized_input.get("tool_name")
        nested_input = normalized_input.get("tool_input")
        return canonical_tool_name(
            nested_name if isinstance(nested_name, str) else None,
            nested_input,
        )

    ledger_name = _LEDGER_SHELL_ALIASES.get(tool_name, tool_name)
    shell_name = canonicalize_shell_tool_name(ledger_name)
    normalized_name = normalize_acp_tool_name(shell_name)
    return normalized_name, normalized_input


def pending_exec_command(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Project the inner command from a pending Codex exec wrapper."""
    if tool_name not in {"exec", "functions.exec", "exec_command"}:
        return None
    raw: Any = tool_input.get("raw") if tool_name in {"exec", "functions.exec"} else tool_input
    return extract_functions_exec_command(raw)


def is_commit_producing(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Return whether successful output may contain commit evidence."""
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return False
        try:
            command = shlex.join(shlex.split(command, comments=True))
        except ValueError:
            pass
        from gobby.workflows.commit_guard import parse_git_commit_invocations

        return bool(parse_git_commit_invocations(command))

    return (
        tool_name in {"mcp gobby-tasks:close_task", "mcp gobby-tasks:link_commit"}
        and isinstance(tool_input.get("commit_sha"), str)
        and bool(tool_input["commit_sha"])
    )


def commit_outcome(tool_name: str, tool_input: dict[str, Any], output: str | None) -> str | None:
    """Extract successful commit evidence only from commit-producing calls."""
    if not is_commit_producing(tool_name, tool_input):
        return None
    commit_sha = tool_input.get("commit_sha")
    if tool_name.startswith("mcp gobby-tasks:") and isinstance(commit_sha, str):
        return f"commit {commit_sha}"
    if not isinstance(output, str):
        return None
    match = _COMMIT_LINE.search(output)
    if match is None:
        return None
    subject = match.group(2).strip()[:80]
    suffix = f" {subject}" if subject else ""
    return f"commit {match.group(1)}{suffix}"


def escape_ledger_text(value: str) -> str:
    """Escape control bytes so each activity entry stays on one physical line."""
    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif codepoint < 32 or codepoint == 127:
            escaped.append(f"\\x{codepoint:02x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def format_tool_activity_line(entry: ToolActivityEntry) -> str:
    """Render one bounded, single-line activity entry."""
    name = escape_ledger_text(str(entry.tool_name))
    primary = _primary_argument(entry.tool_name, entry.tool_input)
    line = f"- {name}"
    if primary:
        line += f" {escape_ledger_text(primary)[:160]}"
    if entry.outcome:
        line += f" → {escape_ledger_text(str(entry.outcome))[:160]}"
    if entry.error is not None:
        line += f" ! failed: {escape_ledger_text(str(entry.error))[:160]}"
    elif not entry.resolved:
        line += " (no result recorded)"
    return line


def render_tool_activity(entries: list[ToolActivityEntry]) -> str:
    """Render a collapsed, evidence-aware activity ledger under both caps."""
    if not entries:
        return ACTIVITY_HEADER
    collapsed = _collapse_entries(entries)
    if _fits(collapsed, omitted_count=0):
        return _join_lines(collapsed)

    protected = _protected_indexes(collapsed)
    selected = set(protected)
    while selected and not _fits_selection(collapsed, selected):
        oldest = min(selected)
        selected.remove(oldest)

    for index in range(len(collapsed)):
        if index in selected:
            continue
        candidate = selected | {index}
        if _fits_selection(collapsed, candidate):
            selected = candidate

    return _join_selection(collapsed, selected)


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _content_has_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return (
        any(
            isinstance(block, Mapping)
            and block.get("type") in {"text", "input_text", "output_text"}
            and isinstance(block.get("text"), str)
            and bool(block["text"].strip())
            for block in value
        )
        if isinstance(value, list)
        else False
    )


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            str(block.get("text") or block.get("content") or "")
            for block in value
            if isinstance(block, Mapping)
        )
    return str(value) if value is not None else ""


def _resolve_entry(entry: ToolActivityEntry, result: Any, content: Any) -> None:
    entry.resolved = True
    error = _result_error(result)
    if error is not None:
        entry.error = error
        return
    if is_commit_producing(entry.tool_name, entry.tool_input):
        entry.outcome = commit_outcome(entry.tool_name, entry.tool_input, _content_text(content))


def _result_error(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    if status in {"error", "cancelled", "failed"}:
        output = value.get("output")
        if isinstance(output, Mapping):
            nested = output.get("error") or output.get("message")
            if nested is not None:
                return _content_text(nested)
        return _content_text(value.get("error") or value.get("message") or status)
    if value.get("is_error") is True or value.get("isError") is True:
        return _content_text(value.get("error") or value.get("message") or value)
    if value.get("success") is False or "error" in value:
        return _content_text(value.get("error") or value.get("message") or value)
    nested = value.get("toolCallResult")
    if isinstance(nested, Mapping):
        nested_error = _result_error(nested)
        if nested_error is not None:
            return nested_error
    response = value.get("response")
    if isinstance(response, Mapping) and "error" in response:
        return _content_text(response.get("error"))
    return None


def _is_user_text_record(cli_name: str, turn: dict[str, Any]) -> bool:
    if cli_name == "codex":
        payload = turn.get("payload")
        return (
            isinstance(payload, Mapping)
            and payload.get("type") == "message"
            and payload.get("role") == "user"
            and _content_has_text(payload.get("content"))
        )
    message = turn.get("message")
    if not isinstance(message, Mapping):
        return False
    if cli_name == "qwen":
        return turn.get("type") == "user" and _qwen_parts_have_text(message.get("parts"))
    if cli_name == "droid":
        return (
            turn.get("type") == "message"
            and message.get("role") == "user"
            and _content_has_text(message.get("content"))
        )
    return False


def _qwen_parts_have_text(value: Any) -> bool:
    return isinstance(value, list) and any(
        isinstance(part, Mapping) and isinstance(part.get("text"), str) and bool(part["text"])
        for part in value
    )


def _partition_entries(
    entries: list[ToolActivityEntry], user_indexes: list[int]
) -> dict[int, list[ToolActivityEntry]]:
    buckets: dict[int, list[ToolActivityEntry]] = {index: [] for index in user_indexes}
    for entry in entries:
        owner = next(
            (index for index in reversed(user_indexes) if index <= entry.record_index),
            None,
        )
        if owner is not None:
            buckets[owner].append(entry)
    return buckets


def _suppress_item_wrappers(
    items: list[ToolActivityEntry], wrappers: list[ToolActivityEntry]
) -> list[ToolActivityEntry]:
    remaining = list(wrappers)
    for item in items:
        for index, wrapper in enumerate(remaining):
            if _entries_match(item, wrapper):
                remaining.pop(index)
                break
    return remaining


def _entries_match(left: ToolActivityEntry, right: ToolActivityEntry) -> bool:
    if left.tool_name != right.tool_name:
        return False
    if left.tool_name == "Bash":
        if left.tool_input.get("command") != right.tool_input.get("command"):
            return False
        if not right.resolved:
            return True
        return (left.error is None) == (right.error is None)
    if left.tool_name.startswith("mcp "):
        return left.tool_input == right.tool_input
    return False


def _mapping_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(decoded, Mapping):
            return dict(decoded)
    return {}


def _primary_argument(tool_name: str, tool_input: dict[str, Any]) -> str:
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if value is not None:
            return str(value)
    command = tool_input.get("command")
    if command is not None:
        return str(command)
    for key in ("pattern", "query"):
        value = tool_input.get(key)
        if value is not None:
            return repr(str(value))
    if "skill" in tool_name.lower() and tool_input.get("name") is not None:
        return f"name={tool_input['name']}"
    if tool_name.startswith("mcp gobby-tasks:"):
        parts: list[str] = []
        for key in ("task_id", "commit_sha"):
            value = tool_input.get(key)
            if value is not None:
                parts.append(f"{key}={value}")
        title = tool_input.get("title")
        if title is not None:
            parts.append(f"title={str(title)[:60]}")
        return " ".join(parts)
    return ""


def _collapse_entries(entries: list[ToolActivityEntry]) -> list[_CollapsedLine]:
    groups: list[list[ToolActivityEntry]] = []
    lines: list[str] = []
    for entry in entries:
        line = format_tool_activity_line(entry)
        if lines and lines[-1] == line:
            groups[-1].append(entry)
            continue
        lines.append(line)
        groups.append([entry])
    return [
        _CollapsedLine(
            line=f"{line} (x{len(group)})" if len(group) > 1 else line,
            entries=tuple(group),
        )
        for line, group in zip(lines, groups, strict=True)
    ]


def _protected_indexes(lines: list[_CollapsedLine]) -> set[int]:
    protected = set(range(max(0, len(lines) - DIGEST_ACTIVITY_TAIL_LINES), len(lines)))
    seen_paths: set[str] = set()
    for index, item in enumerate(lines):
        for entry in item.entries:
            path = next(
                (str(entry.tool_input[key]) for key in _PATH_KEYS if key in entry.tool_input),
                None,
            )
            if entry.error is not None:
                protected.add(index)
            if entry.tool_name in _EDIT_TOOLS and path is not None and path not in seen_paths:
                protected.add(index)
                seen_paths.add(path)
            if _is_task_mutation(entry.tool_name) or is_commit_producing(
                entry.tool_name, entry.tool_input
            ):
                protected.add(index)
    return protected


def _is_task_mutation(tool_name: str) -> bool:
    if not tool_name.startswith("mcp gobby-tasks:"):
        return False
    return tool_name.rsplit(":", maxsplit=1)[-1] in _TASK_MUTATIONS


def _join_lines(lines: list[_CollapsedLine]) -> str:
    return "\n".join([ACTIVITY_HEADER, *(item.line for item in lines)])


def _fits(lines: list[_CollapsedLine], omitted_count: int) -> bool:
    marker = [f"- … {omitted_count} more tool calls omitted"] if omitted_count else []
    rendered = "\n".join([ACTIVITY_HEADER, *(item.line for item in lines), *marker])
    return len(rendered.splitlines()) <= DIGEST_ACTIVITY_MAX_LINES and len(rendered) <= (
        DIGEST_ACTIVITY_MAX_CHARS
    )


def _fits_selection(lines: list[_CollapsedLine], selected: set[int]) -> bool:
    chosen = [item for index, item in enumerate(lines) if index in selected]
    omitted = sum(item.count for index, item in enumerate(lines) if index not in selected)
    return _fits(chosen, omitted)


def _join_selection(lines: list[_CollapsedLine], selected: set[int]) -> str:
    chosen = [item for index, item in enumerate(lines) if index in selected]
    omitted = sum(item.count for index, item in enumerate(lines) if index not in selected)
    marker = f"- … {omitted} more tool calls omitted"
    return "\n".join([ACTIVITY_HEADER, *(item.line for item in chosen), marker])
