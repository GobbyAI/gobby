"""Task-state normalization for plan coverage records."""

from collections.abc import Mapping

from gobby.tasks.state_semantics import TASK_STATES, projected_task_state


def coerce_task_state(value: object, *, default: str = "unknown") -> str:
    """Return a lifecycle state without treating malformed records as active."""
    if isinstance(value, str):
        return value if value in TASK_STATES else default
    if not isinstance(value, Mapping):
        return default
    if "name" in value:
        name = value["name"]
        return name if isinstance(name, str) and name in TASK_STATES else default
    if "is_closed" in value and not isinstance(value["is_closed"], bool):
        return default
    if "is_escalated" in value and not isinstance(value["is_escalated"], bool):
        return default
    if value.get("is_closed") is True:
        return "closed"
    if value.get("is_escalated") is True:
        return "escalated"
    if value.get("is_closed") is False and value.get("is_escalated") is False:
        return projected_task_state({"state": dict(value)})
    return default
