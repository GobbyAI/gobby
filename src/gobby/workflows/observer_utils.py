"""Shared helpers for workflow observer modules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any

from gobby.hooks.tool_outcomes import ToolOutcome, normalize_tool_outcome

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent


def _successful_close_result(tool_output: object) -> Mapping[str, Any] | None:
    """Return the authoritative successful close envelope."""
    if not isinstance(tool_output, Mapping):
        return None
    if tool_output.get("error") or tool_output.get("status") == "error":
        return None
    result = tool_output.get("result")
    if isinstance(result, Mapping) and result.get("error"):
        return None
    if tool_output.get("closed") is True:
        return tool_output
    if isinstance(result, Mapping) and result.get("closed") is True:
        return result
    return None


def _json_safe(value: Any) -> Any:
    """Convert observer-tracked values into JSON-safe session-variable data."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    return str(value)


def _extract_shell_output_text(tool_output: Any) -> str:
    """Extract text content from tool_output, handling both str and dict forms.

    After normalization, ``tool_output`` may be:
    - A plain string, e.g. ``"[main abc1234] Fix bug\\n 1 file changed"``
    - A dict parsed from JSON, e.g. ``{"output": "...", "exitCode": 0}``

    Returns the extracted text, or an empty string when nothing usable exists.
    """
    if isinstance(tool_output, str):
        return tool_output
    if isinstance(tool_output, dict):
        for key in ("output", "stdout", "content"):
            val = tool_output.get(key)
            if isinstance(val, str):
                return val
    return ""


def _extract_shell_command(event: HookEvent) -> str:
    if not event.data:
        return ""
    tool_input = event.data.get("tool_input") or {}
    if isinstance(tool_input, dict):
        command = tool_input.get("command") or tool_input.get("cmd")
        if isinstance(command, str):
            return command
    command = event.data.get("command") or event.data.get("cmd")
    return command if isinstance(command, str) else ""


def _shell_tool_outcome(event: HookEvent) -> ToolOutcome:
    """Return the canonical machine-derived outcome for a shell event."""
    if not event.data:
        return normalize_tool_outcome({})

    is_failure = event.metadata.get("is_failure")
    explicit_success = not is_failure if isinstance(is_failure, bool) else None
    return normalize_tool_outcome(
        event.data,
        explicit_success=explicit_success,
        provenance="hook_event.metadata.is_failure" if explicit_success is not None else None,
    )


def _shell_tool_succeeded(event: HookEvent) -> bool | None:
    """Return a confirmed shell outcome, or ``None`` when no signal exists."""
    return _shell_tool_outcome(event).succeeded
