"""Observer for the daemon-owned interrupt-initiated turn fact."""

from __future__ import annotations

from typing import Any

from gobby.hooks.events import HookEvent, HookEventType
from gobby.sessions.transcript_interrupt import turn_interrupt_initiated

__all__ = ["detect_turn_interrupt"]

_TURN_END_EVENTS = frozenset(
    {
        HookEventType.AFTER_AGENT.value,
        HookEventType.STOP.value,
        HookEventType.STOP_FAILURE.value,
    }
)


def detect_turn_interrupt(event: HookEvent, variables: dict[str, Any]) -> None:
    """Overwrite the interrupt fact before every semantic turn-end evaluation."""
    event_type = (
        event.event_type.value
        if isinstance(event.event_type, HookEventType)
        else str(event.event_type)
    )
    if event_type not in _TURN_END_EVENTS:
        return
    data = event.data if isinstance(event.data, dict) else {}
    variables["turn_interrupt_initiated"] = turn_interrupt_initiated(
        event.source,
        data.get("transcript_path"),
    )
