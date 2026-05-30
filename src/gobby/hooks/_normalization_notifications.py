"""Notification payload normalization helpers."""

from collections.abc import Mapping
from typing import Any

_NOTIFICATION_TYPE_FIELDS = ("notification_type", "notificationType", "type", "level", "severity")
_NOTIFICATION_MESSAGE_FIELDS = ("message", "title", "reason")
_NOTIFICATION_SEVERITY_VALUES = frozenset({"info", "warning", "error"})
_DEFAULT_NOTIFICATION_TYPE = "general"
_DEFAULT_NOTIFICATION_MESSAGE = "Notification event received"


def _non_empty_string(value: Any) -> str | None:
    """Return stripped string values that still contain text."""
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if normalized:
        return normalized
    return None


def notification_type_from_payload(data: Mapping[str, Any]) -> str:
    """Select the canonical notification type from provider-specific aliases."""
    for field_name in _NOTIFICATION_TYPE_FIELDS:
        value = _non_empty_string(data.get(field_name))
        if value:
            return value
    return _DEFAULT_NOTIFICATION_TYPE


def notification_message_from_payload(data: Mapping[str, Any]) -> str:
    """Select the canonical notification message from provider-specific aliases."""
    for field_name in _NOTIFICATION_MESSAGE_FIELDS:
        value = _non_empty_string(data.get(field_name))
        if value:
            return value
    return _DEFAULT_NOTIFICATION_MESSAGE


def _notification_severity_from_payload(data: Mapping[str, Any]) -> str | None:
    """Return a valid notification severity from level/severity aliases."""
    for field_name in ("severity", "level"):
        value = _non_empty_string(data.get(field_name))
        if not value:
            continue

        normalized = value.lower()
        if normalized in _NOTIFICATION_SEVERITY_VALUES:
            return normalized
    return None


def normalize_notification_input(data: dict[str, Any]) -> dict[str, Any]:
    """Backfill strict NotificationInput fields in place and return the same dict."""
    data["notification_type"] = notification_type_from_payload(data)
    data["message"] = notification_message_from_payload(data)

    severity = _notification_severity_from_payload(data)
    if severity:
        data["severity"] = severity

    return data
