"""Normalize Codex hook and app-server lifecycle evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType, correlate_hook_lifecycle

REQUEST_USER_INPUT_METHODS = frozenset(
    {
        "item/tool/requestUserInput",
        "item/requestUserInput",
        "turn/requestUserInput",
    }
)


def annotate_codex_hook_event(event: HookEvent, hook_type: str) -> HookEvent:
    """Attach lifecycle evidence exposed by Codex hooks.json."""
    correlate_hook_lifecycle(event)
    if hook_type == "Stop":
        event.turn_disposition = "completed"
    elif hook_type == "Interrupt":
        event.turn_disposition = "user_interrupted"
    elif hook_type == "PermissionRequest" and event.wait_token:
        event.wait_kind = "approval"
    return event


def annotate_codex_app_event(
    event: HookEvent,
    method: str,
    params: Mapping[str, Any],
) -> HookEvent:
    """Attach lifecycle evidence exposed by Codex app-server protocol events."""
    correlate_hook_lifecycle(event)
    if method == "turn/completed":
        status = _turn_status(params)
        if status == "interrupted":
            event.event_type = HookEventType.INTERRUPT
            event.turn_disposition = "user_interrupted"
        elif status in {"failed", "cancelled", "canceled"}:
            event.turn_disposition = "ended_non_user"
        else:
            event.turn_disposition = "completed"
    elif method in REQUEST_USER_INPUT_METHODS and event.wait_token:
        event.wait_kind = "input"
    elif method.endswith("/requestApproval") and event.wait_token:
        event.wait_kind = "approval"
    elif method == "mcpServer/elicitation/request" and event.wait_token:
        event.wait_kind = "input"
    elif method == "serverRequest/resolved" and event.wait_token:
        event.wait_resolution = "ambiguous"
    elif method == "thread/status/changed":
        _annotate_thread_status(event, params)
    return event


def _turn_status(params: Mapping[str, Any]) -> str:
    turn = params.get("turn")
    if isinstance(turn, Mapping):
        status = turn.get("status")
    else:
        status = params.get("status")
    return status.casefold() if isinstance(status, str) else ""


def _annotate_thread_status(event: HookEvent, params: Mapping[str, Any]) -> None:
    status = params.get("status")
    flags: list[str] = []
    if isinstance(status, str):
        flags.append(status)
    elif isinstance(status, Mapping):
        kind = status.get("type")
        if isinstance(kind, str):
            flags.append(kind)
        for key in ("activeFlags", "active_flags", "flags"):
            raw_flags = status.get(key)
            if isinstance(raw_flags, list):
                flags.extend(value for value in raw_flags if isinstance(value, str))
    normalized = {flag.replace("-", "_").casefold() for flag in flags}
    if normalized & {"interrupted", "user_interrupted"}:
        event.event_type = HookEventType.INTERRUPT
        event.turn_disposition = "user_interrupted"
    elif normalized & {"waiting_on_user_input", "awaiting_input", "request_user_input"}:
        if event.wait_token:
            event.wait_kind = "input"
    elif normalized & {
        "waiting_on_approval",
        "awaiting_approval",
        "permission_request",
        "plan_approval",
    }:
        if event.wait_token:
            event.wait_kind = "approval"
