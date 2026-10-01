"""Mark string tool input that is not a JSON object instead of coercing it to {}."""

import json
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

TOOL_INPUT_ERROR_FIELD = "tool_input_error"
TOOL_INPUT_SOURCES = ("tool_input", "toolArgs", "parameters", "args")
_ERROR_CODES = frozenset({"invalid_json", "non_object_json"})


class ToolInputError(TypedDict):
    """Why a tool input is unavailable; it never carries the sender's content."""

    field: str
    code: Literal["invalid_json", "non_object_json"]


def tool_input_error(data: Mapping[str, Any]) -> ToolInputError | None:
    """Return the well-formed unavailable-input marker in *data*, if any."""
    marker = data.get(TOOL_INPUT_ERROR_FIELD)
    if not isinstance(marker, Mapping) or set(marker) != {"field", "code"}:
        return None
    field, code = marker["field"], marker["code"]
    if field not in TOOL_INPUT_SOURCES or code not in _ERROR_CODES:
        return None
    return {"field": field, "code": code}


def tool_input_source(data: Mapping[str, Any]) -> str:
    """Name the field ``tool_input`` is (or will be) aliased from."""
    return next((field for field in TOOL_INPUT_SOURCES if field in data), "tool_input")


def mark_tool_input_unavailable(
    data: dict[str, Any], source: str, code: Literal["invalid_json", "non_object_json"]
) -> None:
    """Drop the unusable input and record why, without retaining its content."""
    data.pop("tool_input", None)
    data[TOOL_INPUT_ERROR_FIELD] = {"field": source, "code": code}


def decode_string_tool_input(data: dict[str, Any], source: str) -> bool:
    """Decode a string ``tool_input`` as JSON, marking it when it does not decode.

    Returns whether ``tool_input`` was a string; the caller marks a decoded
    value that is still not an object once tool-specific coercion has run.
    """
    raw = data.get("tool_input")
    if not isinstance(raw, str):
        return False
    try:
        data["tool_input"] = json.loads(raw)
    except (ValueError, RecursionError):
        mark_tool_input_unavailable(data, source, "invalid_json")
    return True
