"""Event helpers for rule-engine evaluation."""

from typing import Any

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.workflows.definitions import RuleTriggerEvent
from gobby.workflows.enforcement.blocking import is_gobby_call_tool

_TURN_START_EVENT_VALUES = frozenset(
    {
        HookEventType.BEFORE_AGENT.value,
    }
)

_TURN_END_EVENT_VALUES = frozenset(
    {
        HookEventType.AFTER_AGENT.value,
        HookEventType.STOP.value,
        HookEventType.STOP_FAILURE.value,
    }
)
_COMPACT_TURN_END_BYPASS_PENDING = "_compact_turn_end_bypass_pending"
_MANUAL_COMPACT_TRIGGERS = frozenset({"manual", "user", "clear", "compact", "compress"})


def _get_tool_identity(event_data: dict[str, Any]) -> str:
    """Return effective tool identity for consecutive-block tracking.

    For proxied and native MCP calls, returns 'server:tool' so different MCP
    tools are tracked independently. This prevents one failing MCP tool from
    blocking all other MCP tools.
    """
    tool_name = event_data.get("tool_name", "")
    if is_gobby_call_tool(tool_name):
        tool_input = event_data.get("tool_input") or {}
        if isinstance(tool_input, dict):
            server = tool_input.get("server_name", "")
            tool = tool_input.get("tool_name", "")
            if server and tool:
                return f"{server}:{tool}"
        return str(tool_name)
    if isinstance(tool_name, str) and tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return f"{parts[1]}:{parts[2]}"
    return str(tool_name)


def _is_pipeline_direct_mcp_event(event: HookEvent) -> bool:
    """Return True for direct MCP calls emitted by pipeline sessions."""
    if event.source != SessionSource.PIPELINE:
        return False

    tool_name = event.data.get("tool_name", "")
    return is_gobby_call_tool(tool_name)


def _event_value(event_type: HookEventType | str) -> str:
    if isinstance(event_type, HookEventType):
        return event_type.value
    return str(event_type)


def _project_id_from_event(event: HookEvent) -> str | None:
    """Return project_id from the normalized event or its data payload."""
    if event.project_id:
        return event.project_id
    project_id = event.data.get("project_id") if isinstance(event.data, dict) else None
    return project_id if isinstance(project_id, str) and project_id else None


def _is_manual_compact_event(event: HookEvent) -> bool:
    if _event_value(event.event_type) != HookEventType.PRE_COMPACT.value:
        return False

    data = event.data if isinstance(event.data, dict) else {}
    trigger = data.get("trigger")
    trigger_value = getattr(trigger, "value", trigger)
    if isinstance(trigger_value, str) and trigger_value.lower() in _MANUAL_COMPACT_TRIGGERS:
        return True

    return "custom_instructions" in data and data.get("custom_instructions") is not None


def _is_turn_start_event(event_type: HookEventType | str) -> bool:
    return _event_value(event_type) in _TURN_START_EVENT_VALUES


def _is_turn_end_event(event_type: HookEventType | str) -> bool:
    return _event_value(event_type) in _TURN_END_EVENT_VALUES


def _resolve_rule_events(event_type: HookEventType | str) -> list[RuleTriggerEvent]:
    """Resolve an incoming hook event into rule trigger events."""
    resolved: list[RuleTriggerEvent] = []
    raw_value = _event_value(event_type)

    if _is_turn_start_event(raw_value):
        resolved.append(RuleTriggerEvent.TURN_START)

    if _is_turn_end_event(raw_value):
        resolved.append(RuleTriggerEvent.TURN_END)

    try:
        resolved.append(RuleTriggerEvent(raw_value))
    except ValueError:
        pass

    deduped: list[RuleTriggerEvent] = []
    seen: set[RuleTriggerEvent] = set()
    for trigger in resolved:
        if trigger not in seen:
            deduped.append(trigger)
            seen.add(trigger)
    return deduped


def _clear_edit_write_state(variables: dict[str, Any]) -> None:
    """Clear edit/write pending state and stop-block counter."""
    variables["edit_write_pending"] = False
    variables["edit_write_stop_blocks"] = 0


def _is_write_like_event_data(event_data: dict[str, Any]) -> bool:
    """Return True when normalized event data represents a file mutation."""
    return bool(event_data.get("canonical_repo_mutation")) or (
        event_data.get("canonical_tool_kind") == "write"
    )


def _block_tool_name(event: HookEvent) -> str:
    """Return tool identity used in structured block logs."""
    tool_name = _get_tool_identity(event.data)
    return tool_name or "-"
