"""Grok CLI adapter for hook translation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from gobby.adapters.acp_hook_adapter import ACPHookAdapter
from gobby.adapters.capabilities import (
    GROK_EVENT_MAP,
    GROK_HOOK_ALIASES,
    ContextChannel,
)
from gobby.adapters.degradation import (
    persist_kwargs_from_hook_response,
    truncate_context_for_adapter,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
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
        data = deepcopy(input_data)
        aliases = {
            "sessionId": "session_id",
            "workspaceRoot": "workspace_root",
            "transcriptPath": "transcript_path",
            "clientIdentifier": "client_identifier",
            "promptId": "prompt_id",
            "permissionMode": "permission_mode",
            "toolName": "tool_name",
            "toolUseId": "tool_use_id",
            "toolInput": "tool_input",
            "toolInputTruncated": "tool_input_truncated",
            "toolResult": "tool_output",
            "toolResultTruncated": "tool_result_truncated",
            "errorDetails": "error_details",
            "lastAssistantMessage": "last_assistant_message",
            "stopHookActive": "stop_hook_active",
            "subagentId": "subagent_id",
            "subagentType": "subagent_type",
        }
        for native_name, canonical_name in aliases.items():
            if native_name in data and canonical_name not in data:
                data[canonical_name] = deepcopy(data[native_name])
        if "subagent_id" in data:
            data.setdefault("agent_id", data["subagent_id"])
        if "subagent_type" in data:
            data.setdefault("agent_type", data["subagent_type"])
        return super()._normalize_event_data(data)

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Preserve Grok's explicit failure-hook outcome when emitted."""
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data")
        if not hook_type and isinstance(input_data, dict):
            hook_type = input_data.get("hook_event_name") or input_data.get("hookEventName", "")
        if not hook_type:
            hook_type = native_event.get("hook_event_name") or native_event.get("hookEventName", "")
        canonical_hook = self.HOOK_EVENT_NAME_MAP.get(hook_type, hook_type)
        canonical_event = dict(native_event)
        if "input_data" in canonical_event or "hook_type" in canonical_event:
            canonical_event["hook_type"] = canonical_hook
        else:
            canonical_event = {"hook_type": canonical_hook, "input_data": native_event}
        event = super().translate_to_hook_event(canonical_event)
        event.metadata["_native_hook_type"] = hook_type
        if canonical_hook == "post_tool_use_failure":
            event.metadata["is_failure"] = True
            normalize_tool_outcome(
                event.data,
                explicit_success=False,
                provenance="grok.hook:post_tool_use_failure",
            )
        return event

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict[str, Any]:
        """Translate Grok observe-only and recoverable subagent responses."""
        canonical_hook = self.HOOK_EVENT_NAME_MAP.get(hook_type or "", hook_type or "")
        if canonical_hook in {"permission_denied", "stop_failure", "subagent_start"}:
            return {"decision": "allow", "continue": True}

        if canonical_hook == "subagent_stop" and response.decision in {"deny", "block"}:
            from gobby.adapters.base import normalize_adapter_response_reason

            reason = normalize_adapter_response_reason(
                response,
                adapter_name=self.__class__.__name__,
                hook_type=hook_type,
                logger=self._event_logger(),
            )
            result: dict[str, Any] = {
                "continue": True,
                "decision": "block",
                "reason": reason or "Blocked by Gobby hook",
            }
            if response.context:
                result["hookSpecificOutput"] = {
                    "hookEventName": "SubagentStop",
                    "additionalContext": truncate_context_for_adapter(
                        response.context,
                        provider=self.source,
                        hook_type=hook_type,
                        destination_channel=ContextChannel.ADDITIONAL_CONTEXT,
                        contributor_sizes={"response.context": len(response.context)},
                        event_logger=self._event_logger(),
                        **persist_kwargs_from_hook_response(response, self._hook_manager),
                    ),
                }
            return result

        return super().translate_from_hook_response(response, hook_type)


__all__ = ["GrokAdapter"]
