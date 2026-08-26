"""Canonical Codex ``item_completed`` normalization shared by transcript consumers."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from typing import Any, NamedTuple

from gobby.hooks._normalization_mcp import _unwrap_mcp_tool_output


class CommandExecutionOutcome(NamedTuple):
    command: str
    exit_code: int | None
    success: bool | None
    output: str


def normalize_command_execution(item: Any) -> CommandExecutionOutcome | None:
    """Normalize a Codex ``CommandExecution`` item without inventing a verdict."""
    if not isinstance(item, Mapping) or item.get("type") != "CommandExecution":
        return None
    argv = item.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
        return None
    command = argv[-1] if len(argv) >= 3 and argv[-2] in {"-c", "-lc"} else shlex.join(argv)
    raw_exit_code = item.get("exit_code")
    exit_code = (
        raw_exit_code
        if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool)
        else None
    )
    success = exit_code == 0 if exit_code is not None else None
    output = item.get("aggregated_output")
    if not isinstance(output, str):
        stdout = item.get("stdout") if isinstance(item.get("stdout"), str) else ""
        stderr = item.get("stderr") if isinstance(item.get("stderr"), str) else ""
        output = f"{stdout}{stderr}"
    return CommandExecutionOutcome(command, exit_code, success, output)


def mcp_item_failure(item: Any) -> str | None:
    """Return an MCP item's transport or application failure text."""
    if not isinstance(item, Mapping):
        return None
    status = item.get("status")
    result = item.get("result")
    if isinstance(result, Mapping) and "Err" in result:
        return _failure_text(result["Err"])
    if isinstance(result, Mapping) and "Ok" in result:
        result = result["Ok"]
    result = _parse_json(result)
    result = _unwrap_mcp_tool_output(result)
    failure = _structured_failure(result)
    if failure is not None:
        return failure
    if status == "failed":
        return _failure_text(result) or "failed"
    return None


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _structured_failure(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("is_error") is True or value.get("isError") is True:
        return _failure_text(value.get("error") or value.get("message") or value)
    if value.get("success") is False:
        return _failure_text(value.get("error") or value.get("message") or value)
    result = value.get("result")
    if result is not None and result is not value:
        return _structured_failure(_parse_json(result))
    return None


def _failure_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("error", "message", "detail"):
            text = value.get(key)
            if isinstance(text, str) and text:
                return text
        return json.dumps(dict(value), default=str, sort_keys=True)
    return str(value) if value is not None else ""
