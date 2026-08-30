"""Shared AGY hook contract data."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

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

# Consecutive PostInvocation deny→force_continue emissions per execution.
# AGY honors force_continue without a native bound; Gobby owns the cap.
AGY_FORCE_CONTINUE_LIMIT = 10


def agy_execution_num(payload: dict[str, Any]) -> int | None:
    """Return AGY ``executionNum`` from a hook payload, if present."""

    raw: Any = None
    input_data = payload.get("input_data")
    if isinstance(input_data, dict):
        raw = input_data.get("execution_num", input_data.get("executionNum"))
    if raw is None:
        raw = payload.get("execution_num", payload.get("executionNum"))
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw)
    return None


def strip_unbudgeted_force_continue(response: dict[str, Any]) -> dict[str, Any]:
    """Drop force_continue when no durable budget slot backs the emission."""

    if response.get("terminationBehavior") != "force_continue":
        return response
    visible = dict(response)
    visible.pop("terminationBehavior", None)
    return visible


AGY_TOOL_MAP: dict[str, str] = {
    "list_dir": "Ls",
    "run_command": "Bash",
    "view_file": "Read",
    "find_by_name": "Glob",
    "write_to_file": "Write",
    "replace_file_content": "Edit",
    "grep_search": "Grep",
    "call_mcp_tool": "mcp__gobby__call_tool",
}

_AGY_EXIT_RE = re.compile(r"(?m)^[ \t]*The command exited with code (-?\d+)\.[ \t]*$")


AGY_PAYLOAD_ALIASES: dict[str, str] = {
    "conversationId": "session_id",
    "transcriptPath": "transcript_path",
    "workspacePaths": "workspace_paths",
    "artifactDirectoryPath": "artifact_directory_path",
    "modelName": "model",
    "stepIdx": "step_idx",
    "invocationNum": "invocation_num",
    "initialNumSteps": "initial_num_steps",
    "executionNum": "execution_num",
    "terminationReason": "termination_reason",
    "fullyIdle": "fully_idle",
}


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


def decode_agy_tool_args(value: Any) -> Any:
    """Decode AGY tool args, keeping a raw fallback for JSON-string forms."""

    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_agy_command_exit(content: str | None) -> int | None:
    """Parse AGY's free-text `The command exited with code N.` sentence."""

    if not isinstance(content, str):
        return None
    match = _AGY_EXIT_RE.search(content)
    if match is None:
        return None
    return int(match.group(1))


def normalize_agy_tool_name(name: str) -> str:
    """Map an AGY snake_case call name through the shared tool table."""

    return AGY_TOOL_MAP.get(name, name)


def apply_agy_payload_aliases(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy an AGY payload with camelCase keys aliased to Gobby names."""

    data = dict(payload)
    for native_name, canonical_name in AGY_PAYLOAD_ALIASES.items():
        if native_name in data and canonical_name not in data:
            data[canonical_name] = data[native_name]

    tool_call = data.get("toolCall")
    if isinstance(tool_call, dict):
        if "tool_name" not in data and "name" in tool_call:
            data["tool_name"] = tool_call["name"]
        if "tool_input" not in data and "args" in tool_call:
            data["tool_input"] = tool_call["args"]

    if not data.get("cwd"):
        paths = data.get("workspace_paths")
        if isinstance(paths, list) and paths and isinstance(paths[0], str):
            data["cwd"] = paths[0]
    return data
