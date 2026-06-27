"""ACP session config option normalization."""

from __future__ import annotations

from typing import Any


def _string_value(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def normalize_config_option_values(values: Any) -> list[dict[str, Any]]:
    """Return valid config option values in provider order."""
    if not isinstance(values, list):
        return []

    normalized: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        option_value = _string_value(value, "value")
        name = _string_value(value, "name")
        if option_value is None or name is None:
            continue
        item: dict[str, Any] = {
            "value": option_value,
            "name": name,
        }
        description = _string_value(value, "description")
        if description is not None:
            item["description"] = description
        normalized.append(item)
    return normalized


def normalize_config_options(config_options: Any) -> list[dict[str, Any]]:
    """Return valid ACP config options in provider order."""
    if not isinstance(config_options, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw_option in config_options:
        if not isinstance(raw_option, dict):
            continue
        option_id = _string_value(raw_option, "id")
        name = _string_value(raw_option, "name")
        option_type = _string_value(raw_option, "type")
        current_value = _string_value(raw_option, "currentValue")
        values = normalize_config_option_values(raw_option.get("options"))
        if (
            option_id is None
            or name is None
            or option_type is None
            or current_value is None
            or not values
        ):
            continue
        option: dict[str, Any] = {
            "id": option_id,
            "name": name,
            "type": option_type,
            "currentValue": current_value,
            "options": values,
        }
        description = _string_value(raw_option, "description")
        if description is not None:
            option["description"] = description
        category = _string_value(raw_option, "category")
        if category is not None:
            option["category"] = category
        normalized.append(option)
    return normalized
