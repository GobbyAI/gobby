"""Shared canonicalization for the public ``call_tool`` wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.mcp_proxy._coerce_arguments import coerce_string_arguments

CALL_TOOL_WRAPPER_FIELDS = ("server_name", "tool_name", "session_id", "project_id")


@dataclass(frozen=True, slots=True)
class CanonicalCallToolWrapper:
    """Canonicalized wrapper input for public ``call_tool`` entrypoints."""

    server_name: str | None
    tool_name: str | None
    arguments: dict[str, Any] | None
    session_id: str | None
    project_id: str | None


class CallToolWrapperInputError(ValueError):
    """Raised when the wrapper receives invalid JSON in ``arguments``/``args``."""

    def __init__(self, field_name: str, raw_value: str):
        self.field_name = field_name
        self.raw_value = raw_value
        super().__init__(f"Invalid JSON in '{field_name}' parameter: {raw_value[:200]}")


def _coerce_wrapper_arguments(
    value: str | dict[str, Any] | None,
    *,
    field_name: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)

    parsed = coerce_string_arguments(value)
    if parsed is None:
        raise CallToolWrapperInputError(field_name, value)
    return parsed


def _pick_wrapper_value(top_level: str | None, nested: Any) -> str | None:
    if isinstance(top_level, str) and top_level:
        return top_level
    if isinstance(nested, str) and nested:
        return nested
    return None


def canonicalize_call_tool_wrapper(
    *,
    server_name: str | None,
    tool_name: str | None,
    arguments: str | dict[str, Any] | None = None,
    args: str | dict[str, Any] | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
) -> CanonicalCallToolWrapper:
    """Canonicalize public ``call_tool`` wrapper inputs.

    Top-level wrapper fields win. Missing top-level wrapper fields are hoisted
    from the ``arguments``/``args`` payload when present, then stripped so only
    inner tool arguments reach downstream dispatch.
    """

    effective_arguments = _coerce_wrapper_arguments(arguments, field_name="arguments")
    if effective_arguments is None and args is not None:
        effective_arguments = _coerce_wrapper_arguments(args, field_name="args")

    canonical_arguments = dict(effective_arguments) if effective_arguments is not None else None
    nested = canonical_arguments or {}

    canonical_server_name = _pick_wrapper_value(server_name, nested.get("server_name"))
    canonical_tool_name = _pick_wrapper_value(tool_name, nested.get("tool_name"))
    canonical_session_id = _pick_wrapper_value(session_id, nested.get("session_id"))
    canonical_project_id = _pick_wrapper_value(project_id, nested.get("project_id"))

    if canonical_arguments is not None:
        for field in CALL_TOOL_WRAPPER_FIELDS:
            canonical_arguments.pop(field, None)

    return CanonicalCallToolWrapper(
        server_name=canonical_server_name,
        tool_name=canonical_tool_name,
        arguments=canonical_arguments,
        session_id=canonical_session_id,
        project_id=canonical_project_id,
    )
