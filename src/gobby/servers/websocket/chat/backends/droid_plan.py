"""Droid plan/spec-mode helpers."""

from __future__ import annotations

import re
from typing import Any

# Droid's JSON-RPC stream currently names the plan-exit tool ``ExitSpecMode`` and
# presents a plan via ``ExitSpecMode`` (Factory "spec mode"); the others cover
# Claude-style ``ExitPlanMode`` and Codex-style ``update_plan`` should Droid ever
# surface them. Compared as alphanumeric-only lowercase so separators/casing in
# the upstream tool name don't matter.
_PLAN_EXIT_TOOL_KEYS = frozenset({"exitspecmode", "exitplanmode", "updateplan"})

# Argument keys, in priority order, that may hold the plan/spec body.
_PLAN_TOOL_ARG_KEYS: tuple[str, ...] = ("plan", "spec", "content", "markdown", "text")

# Tools that execute a command or mutate state. Droid narrates their output as
# assistant content *after* they run (e.g. reformatting ``git status`` into a
# results table). That post-execution narration is not plan content, so once
# such a tool completes the prose-plan capture stops (see ``_closes_plan_capture``).
# Compared as alphanumeric-only lowercase so separators/casing/namespacing in the
# upstream tool name don't matter.
_PLAN_CAPTURE_CLOSING_TOOLS = frozenset(
    {
        "bash",
        "shell",
        "sh",
        "exec",
        "execcommand",
        "runcommand",
        "command",
        "edit",
        "write",
        "multiedit",
        "notebookedit",
        "applypatch",
        "strreplace",
        "strreplaceeditor",
        "strreplacebasededittool",
    }
)

# A plan body is "present" once the captured prose has a markdown heading or
# enough substance. Guards capture-close so a research-first turn (run a read
# command, then present the plan) still surfaces the plan.
_PLAN_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s")
_PLAN_CAPTURE_MIN_CHARS = 160


def _is_plan_exit_tool(tool_name: str) -> bool:
    """Whether ``tool_name`` is a plan-exit/spec tool carrying a plan body."""
    key = "".join(ch for ch in tool_name.lower() if ch.isalnum())
    return key in _PLAN_EXIT_TOOL_KEYS


def _closes_plan_capture(tool_name: str, captured: str) -> bool:
    """Whether a completed tool ends prose-plan capture for this turn.

    Command/mutation tools narrate their output as assistant content *after*
    they run, which is execution narration rather than plan. Once such a tool
    completes and a plan body is already captured, stop accumulating prose so
    the post-execution narration does not leak into the broadcast plan
    (#15724). Guarded on a plan already being present so a research-first turn
    (read command, then present the plan) still surfaces the plan.
    """
    key = "".join(ch for ch in tool_name.lower() if ch.isalnum())
    if key not in _PLAN_CAPTURE_CLOSING_TOOLS:
        return False
    return (
        bool(_PLAN_HEADING_RE.search(captured)) or len(captured.strip()) >= _PLAN_CAPTURE_MIN_CHARS
    )


def _extract_plan_from_tool_args(arguments: dict[str, Any]) -> str | None:
    """Pull the plan/spec body from a plan-exit tool's arguments.

    Tries the known plan argument keys in priority order and returns the first
    non-empty string. Returns ``None`` when the tool carries no plan body (some
    CLIs use the tool purely as an exit signal), so the caller falls back to the
    accumulated assistant prose.
    """
    for key in _PLAN_TOOL_ARG_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


__all__ = [
    "_closes_plan_capture",
    "_extract_plan_from_tool_args",
    "_is_plan_exit_tool",
]
