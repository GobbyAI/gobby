"""Argument validation and normalization helpers for the tool proxy service."""

import json as _json
from typing import Any

from gobby.mcp_proxy.models import ToolProxyErrorCode


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
    del exception
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
