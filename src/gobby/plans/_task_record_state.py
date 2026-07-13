"""Task-state normalization for plan coverage records."""

from collections.abc import Mapping


def coerce_task_state(value: object, *, default: str = "unknown") -> str:
    """Return a lifecycle state without treating malformed records as active."""
    if isinstance(value, str):
        return value or default
    if not isinstance(value, Mapping):
        return default
    if "is_closed" in value and not isinstance(value["is_closed"], bool):
        return default
    if "is_escalated" in value and not isinstance(value["is_escalated"], bool):
        return default
    if value.get("is_closed") is True:
        return "closed"
    if value.get("is_escalated") is True:
        return "escalated"
    current_stage = value.get("current_stage")
    stage_state = current_stage.get("state") if isinstance(current_stage, Mapping) else None
    return stage_state if isinstance(stage_state, str) and stage_state else default
