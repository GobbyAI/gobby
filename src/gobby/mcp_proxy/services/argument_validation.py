"""Argument validation and normalization helpers for the tool proxy service."""

import json as _json
from typing import Any

from gobby.mcp_proxy.models import ToolProxyErrorCode

_SUPPORTED_JSON_TYPES = frozenset(
    {"string", "number", "integer", "boolean", "object", "array", "null"}
)


def _matches_json_type(value: Any, json_type: str) -> bool:
    """Return whether a Python value matches a supported JSON Schema type."""
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer()
        )
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "object":
        return isinstance(value, dict)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "null":
        return value is None
    return False


def _evaluate_declared_types(
    value: Any,
    declaration: Any,
) -> tuple[bool | None, list[str]]:
    """Evaluate a scalar or list-valued type declaration.

    None means the declaration cannot be enforced by this type-only validator.
    """
    if isinstance(declaration, str):
        if declaration not in _SUPPORTED_JSON_TYPES:
            return None, []
        return _matches_json_type(value, declaration), [declaration]

    if not isinstance(declaration, list):
        return None, []

    supported: list[str] = []
    has_unsupported = False
    for item in declaration:
        if not isinstance(item, str) or item not in _SUPPORTED_JSON_TYPES:
            has_unsupported = True
            continue
        if item not in supported:
            supported.append(item)

    if any(_matches_json_type(value, json_type) for json_type in supported):
        return True, supported
    if has_unsupported or not supported:
        return None, supported
    return False, supported


def _evaluate_type_schema(
    value: Any,
    schema: Any,
) -> tuple[bool | None, list[str]]:
    """Evaluate supported type constraints, including recursive anyOf branches."""
    if not isinstance(schema, dict):
        return None, []

    checks: list[bool | None] = []
    expected_types: list[str] = []

    if "type" in schema:
        type_match, declared_types = _evaluate_declared_types(value, schema["type"])
        checks.append(type_match)
        expected_types.extend(declared_types)

    if "anyOf" in schema:
        branches = schema["anyOf"]
        if not isinstance(branches, list) or not branches:
            checks.append(None)
        else:
            branch_checks: list[bool | None] = []
            for branch in branches:
                branch_match, branch_types = _evaluate_type_schema(value, branch)
                branch_checks.append(branch_match)
                for branch_type in branch_types:
                    if branch_type not in expected_types:
                        expected_types.append(branch_type)

            if any(result is True for result in branch_checks):
                checks.append(True)
            elif any(result is None for result in branch_checks):
                checks.append(None)
            else:
                checks.append(False)

    if not checks:
        return None, expected_types
    if any(result is False for result in checks):
        return False, expected_types
    if all(result is True for result in checks):
        return True, expected_types
    return None, expected_types


def _json_type_name(value: Any) -> str:
    """Return the JSON type name used in validation errors."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    return type(value).__name__


def check_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate arguments against JSON schema."""
    errors = []
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for key in arguments:
        if key not in properties:
            similar = [p for p in properties if p in key or key in p]
            if similar:
                errors.append(f"Unknown parameter '{key}'. Did you mean '{similar[0]}'?")
            else:
                valid_params = list(properties.keys())
                errors.append(f"Unknown parameter '{key}'. Valid parameters: {valid_params}")
            continue

        type_match, expected_types = _evaluate_type_schema(arguments[key], properties[key])
        if type_match is False and expected_types:
            expected = " or ".join(expected_types)
            actual = _json_type_name(arguments[key])
            errors.append(f"Invalid type for parameter '{key}': expected {expected}, got {actual}")

    for req in required:
        if req not in arguments:
            errors.append(f"Missing required parameter '{req}'")

    return errors


def is_argument_error(error_message: str) -> bool:
    """Detect if error message suggests invalid arguments."""
    indicators = [
        "parameter",
        "argument",
        "required",
        "missing",
        "invalid",
        "unknown",
        "expected",
        "type error",
        "validation",
        "schema",
        "property",
        "field",
        "400",
        "422",
        "-32602",
    ]
    error_lower = error_message.lower()
    return any(indicator in error_lower for indicator in indicators)


def classify_error(service: Any, error_message: str, exception: Exception) -> str:
    """Classify an error into a structured tool proxy error code."""
    if isinstance(exception, TimeoutError):
        return ToolProxyErrorCode.CONNECTION_ERROR.value

    error_lower = error_message.lower()

    if "server" in error_lower:
        if "not found" in error_lower:
            return ToolProxyErrorCode.SERVER_NOT_FOUND.value
        if "not configured" in error_lower:
            return ToolProxyErrorCode.SERVER_NOT_CONFIGURED.value

    if "tool" in error_lower and "not found" in error_lower:
        return ToolProxyErrorCode.TOOL_NOT_FOUND.value

    if service._is_argument_error(error_message):
        return ToolProxyErrorCode.INVALID_ARGUMENTS.value

    connection_indicators = ["connection", "timeout", "refused", "unreachable", "circuit"]
    if any(ind in error_lower for ind in connection_indicators):
        return ToolProxyErrorCode.CONNECTION_ERROR.value

    return ToolProxyErrorCode.EXECUTION_ERROR.value


def prepare_arguments(
    arguments: dict[str, Any] | str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize tool arguments to a dict or return a structured error response."""
    arguments = arguments or {}

    if isinstance(arguments, str):
        from gobby.mcp_proxy._coerce_arguments import coerce_string_arguments

        parsed = coerce_string_arguments(arguments)
        if parsed is not None:
            arguments = parsed
        else:
            try:
                val = _json.loads(arguments)
                type_name = type(val).__name__
            except (ValueError, TypeError):
                type_name = None

            if type_name:
                error_msg = f"Invalid arguments: expected dict, got {type_name}"
            else:
                error_msg = "Invalid arguments: expected dict, got string that isn't valid JSON"
            return None, {
                "success": False,
                "error": error_msg,
                "error_code": ToolProxyErrorCode.INVALID_ARGUMENTS.value,
            }

    if isinstance(arguments, dict):
        return dict(arguments), None

    return None, {
        "success": False,
        "error": f"Invalid arguments: expected dict, got {type(arguments).__name__}",
        "error_code": ToolProxyErrorCode.INVALID_ARGUMENTS.value,
    }
