"""Grok CLI adapter for hook translation."""

from __future__ import annotations

from typing import Any

from gobby.adapters.acp_hook_adapter import ACPHookAdapter
from gobby.adapters.capabilities import GROK_EVENT_MAP, GROK_HOOK_ALIASES
from gobby.hooks.events import HookEventType, SessionSource


class GrokAdapter(ACPHookAdapter):
    """Adapter for Grok CLI hook translation."""

    @property
    def source(self) -> SessionSource:
        return SessionSource.GROK

    EVENT_MAP: dict[str, HookEventType] = dict(GROK_EVENT_MAP)
    HOOK_EVENT_NAME_MAP: dict[str, str] = dict(GROK_HOOK_ALIASES)
    TOOL_MAP: dict[str, str] = {
        "run_terminal_command": "Bash",
        "run_terminal_cmd": "Bash",
        "terminal": "Bash",
        "bash": "Bash",
        "read_file": "Read",
        "write_file": "Write",
        "edit_file": "Edit",
        "search_replace": "Edit",
        "grep": "Grep",
        "grep_search": "Grep",
        "glob": "Glob",
        "list_directory": "Ls",
        "ls": "Ls",
        "web_fetch": "Fetch",
    }

    def _normalize_event_data(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Grok's camel-case tool payload fields."""
        data = dict(input_data)
        if "toolInput" in data and "tool_input" not in data:
            data["tool_input"] = data["toolInput"]
        if "toolResult" in data and "tool_output" not in data:
            data["tool_output"] = data["toolResult"]
        return super()._normalize_event_data(data)


__all__ = ["GrokAdapter"]
