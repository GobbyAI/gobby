"""Grok CLI adapter for hook translation."""

from __future__ import annotations

from typing import Any

from gobby.adapters.acp_hook_adapter import ACPHookAdapter
from gobby.adapters.capabilities import GROK_EVENT_MAP, GROK_HOOK_ALIASES
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_outcome


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

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Preserve Grok's explicit failure-hook outcome when emitted."""
        event = super().translate_to_hook_event(native_event)
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data")
        if not hook_type and isinstance(input_data, dict):
            hook_type = input_data.get("hook_event_name") or input_data.get("hookEventName", "")
        if not hook_type:
            hook_type = native_event.get("hook_event_name") or native_event.get("hookEventName", "")
        canonical_hook = self.HOOK_EVENT_NAME_MAP.get(hook_type, hook_type)
        if canonical_hook == "post_tool_use_failure":
            event.metadata["is_failure"] = True
            normalize_tool_outcome(
                event.data,
                explicit_success=False,
                provenance="grok.hook:post_tool_use_failure",
            )
        return event


__all__ = ["GrokAdapter"]
