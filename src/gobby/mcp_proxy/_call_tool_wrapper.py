"""Shared canonicalization for the public ``call_tool`` wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.mcp_proxy._coerce_arguments import coerce_string_arguments

CALL_TOOL_WRAPPER_FIELDS = ("server_name", "tool_name", "project_id")
CALL_TOOL_ARGUMENT_FIELDS = ("arguments", "args")


@dataclass(frozen=True, slots=True)
class CanonicalCallToolWrapper:
    """Canonicalized wrapper input for public ``call_tool`` entrypoints."""

    server_name: str | None
    tool_name: str | None
    arguments: str | dict[str, Any] | None
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


def _has_top_level_wrapper_value(value: str | None) -> bool:
    return isinstance(value, str) and bool(value)


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

    Top-level wrapper fields win. Missing server/tool/project wrapper fields are
    hoisted from the ``arguments``/``args`` payload when present. If the hoisted
    payload also contains nested ``arguments``/``args``, that nested value becomes
    the target tool arguments. Wrapper fields are then stripped so only inner tool
    arguments reach downstream dispatch. Top-level ``session_id`` is wrapper
    context for normal same-repo calls; downstream dispatch may inject its
    resolved UUID when a target schema requires ``session_id``.
    ``arguments.session_id`` is a target-tool override for a different session:
    local ``#N`` refs resolve in the caller project, while cross-project targets
    should use UUIDs. If routing fields are already top-level, malformed string
    arguments are preserved for target validation.
    """

    raw_argument_value = arguments if arguments is not None else args
    raw_argument_field = "arguments" if arguments is not None else "args"
    try:
        effective_arguments = _coerce_wrapper_arguments(
            raw_argument_value,
            field_name=raw_argument_field,
        )
    except CallToolWrapperInputError:
        if server_name and tool_name:
            return CanonicalCallToolWrapper(
                server_name=server_name,
                tool_name=tool_name,
                arguments=raw_argument_value,
                session_id=session_id if isinstance(session_id, str) and session_id else None,
                project_id=project_id if isinstance(project_id, str) and project_id else None,
            )
        raise

    canonical_arguments: str | dict[str, Any] | None = (
        dict(effective_arguments) if effective_arguments is not None else None
    )
    nested = canonical_arguments if isinstance(canonical_arguments, dict) else {}

    server_name_from_nested = (
        not _has_top_level_wrapper_value(server_name)
        and isinstance(nested.get("server_name"), str)
        and bool(nested.get("server_name"))
    )
    tool_name_from_nested = (
        not _has_top_level_wrapper_value(tool_name)
        and isinstance(nested.get("tool_name"), str)
        and bool(nested.get("tool_name"))
    )
    canonical_server_name = _pick_wrapper_value(server_name, nested.get("server_name"))
    canonical_tool_name = _pick_wrapper_value(tool_name, nested.get("tool_name"))
    canonical_session_id = session_id if isinstance(session_id, str) and session_id else None
    canonical_project_id = _pick_wrapper_value(project_id, nested.get("project_id"))

    # Only unwrap nested payloads when routing came from the nested wrapper; top-level
    # server/tool values preserve malformed target arguments for downstream validation.
    if (server_name_from_nested or tool_name_from_nested) and isinstance(canonical_arguments, dict):
        for field in CALL_TOOL_ARGUMENT_FIELDS:
            if field in canonical_arguments:
                raw_nested_arguments = canonical_arguments[field]
                if raw_nested_arguments is None:
                    canonical_arguments = {}
                elif isinstance(raw_nested_arguments, dict):
                    canonical_arguments = dict(raw_nested_arguments)
                else:
                    canonical_arguments = raw_nested_arguments
                break

    if canonical_arguments is not None:
        for field in CALL_TOOL_WRAPPER_FIELDS:
            if isinstance(canonical_arguments, dict):
                canonical_arguments.pop(field, None)

    return CanonicalCallToolWrapper(
        server_name=canonical_server_name,
        tool_name=canonical_tool_name,
        arguments=canonical_arguments,
        session_id=canonical_session_id,
        project_id=canonical_project_id,
    )
