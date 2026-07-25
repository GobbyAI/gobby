"""Validation and precedence for Telegram Bot API link preview options."""

from __future__ import annotations

from collections.abc import Mapping

_BOOLEAN_FIELDS = frozenset(
    {
        "is_disabled",
        "prefer_small_media",
        "prefer_large_media",
        "show_above_text",
    }
)
_ALLOWED_FIELDS = _BOOLEAN_FIELDS | {"url"}


def normalize_link_preview_options(
    value: object,
    *,
    field_name: str,
) -> dict[str, bool | str] | None:
    """Validate a LinkPreviewOptions value and return a detached JSON object."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object or null")

    unknown_fields = {key for key in value if key not in _ALLOWED_FIELDS}
    if unknown_fields:
        unknown = ", ".join(sorted(str(key) for key in unknown_fields))
        raise ValueError(f"{field_name} contains unsupported fields: {unknown}")

    normalized: dict[str, bool | str] = {}
    for key, item in value.items():
        if key in _BOOLEAN_FIELDS:
            if not isinstance(item, bool):
                raise ValueError(f"{field_name}.{key} must be a boolean")
            normalized[key] = item
        elif key == "url":
            if not isinstance(item, str):
                raise ValueError(f"{field_name}.url must be a string")
            normalized[key] = item
    return normalized


def resolve_link_preview_options(
    default: Mapping[str, bool | str] | None,
    message_metadata: Mapping[str, object],
) -> dict[str, bool | str] | None:
    """Merge a per-message override over the channel default; null clears it."""
    if "link_preview_options" not in message_metadata:
        return dict(default) if default is not None else None

    override = normalize_link_preview_options(
        message_metadata["link_preview_options"],
        field_name="message link_preview_options",
    )
    if override is None:
        return None
    return {**(default or {}), **override}
