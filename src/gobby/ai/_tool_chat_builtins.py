"""Structured builtin tools and canonical tool-result serialization."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict

_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_EVIDENCE_REF_BYTES = 16
_RESULT_TOO_LARGE_CODE = "tool_result_too_large"
_RESULT_TOO_LARGE_MESSAGE = "result exceeds cap"


@dataclass(frozen=True, kw_only=True)
class BuiltinExecutionContext:
    """Runtime-owned limits and identity supplied to one builtin invocation."""

    max_payload_bytes: int
    evidence_ref: str
    subprocess_deadline: float


@dataclass(frozen=True, kw_only=True)
class BuiltinToolResult:
    """Structured builtin outcome; serialization remains runtime-owned."""

    payload: object = None
    error_code: str | None = None
    error: str | None = None
    details: Mapping[str, object] | None = None
    selector: object | None = None
    range: object | None = None
    complete: bool | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        has_code = self.error_code is not None
        has_message = self.error is not None
        if has_code != has_message:
            raise ValueError("BuiltinToolResult errors require both error_code and error")

    @property
    def ok(self) -> bool:
        return self.error_code is None


BuiltinToolHandler = Callable[
    [dict[str, Any], BuiltinExecutionContext], Coroutine[Any, Any, BuiltinToolResult]
]


@dataclass(frozen=True, kw_only=True)
class BuiltinToolSpec:
    """One provider-renderable builtin tool and its async handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: BuiltinToolHandler


class InvocationRecord(TypedDict):
    """One runtime-observed tool invocation."""

    tool_name: str
    arguments: object
    result_size_bytes: int
    ok: bool
    error_code: str | None
    evidence_ref: str | None
    selector: NotRequired[object]
    range: NotRequired[object]
    complete: NotRequired[bool]
    content_hash: NotRequired[str]


def canonical_json(value: object) -> str:
    """Serialize one JSON value with the runtime's canonical encoding."""

    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_size(value: object) -> int:
    """Return the exact UTF-8 size produced by :func:`canonical_json`."""

    return len(canonical_json(value).encode("utf-8"))


def builtin_result_envelope(
    result: BuiltinToolResult, *, evidence_ref: str | None = None
) -> dict[str, object]:
    """Build the canonical provider-facing envelope for a builtin result."""

    if result.ok:
        envelope: dict[str, object] = {"success": True, "result": result.payload}
        if evidence_ref is not None:
            envelope["evidence_ref"] = evidence_ref
        return envelope

    envelope = {
        "success": False,
        "error_code": result.error_code,
        "error": result.error,
    }
    if result.details:
        envelope["details"] = dict(result.details)
    return envelope


def serialize_builtin_tool_result(
    result: BuiltinToolResult, *, evidence_ref: str | None = None
) -> str:
    """Serialize a builtin result using the canonical provider encoding."""

    return canonical_json(builtin_result_envelope(result, evidence_ref=evidence_ref))


def serialized_builtin_tool_result_size(
    result: BuiltinToolResult, *, evidence_ref: str | None = None
) -> int:
    """Measure the exact bytes returned by :func:`serialize_builtin_tool_result`."""

    return len(serialize_builtin_tool_result(result, evidence_ref=evidence_ref).encode("utf-8"))


def tool_result_too_large() -> BuiltinToolResult:
    """Return the minimum legal typed error for an oversized tool result."""

    return BuiltinToolResult(
        error_code=_RESULT_TOO_LARGE_CODE,
        error=_RESULT_TOO_LARGE_MESSAGE,
    )


def minimum_typed_error_result_size() -> int:
    """Return the minimum configured cap that can carry a typed size error."""

    return serialized_builtin_tool_result_size(tool_result_too_large())


def success_payload_capacity(byte_cap: int, evidence_ref: str) -> int:
    """Return the exact standalone payload budget within a success envelope."""

    empty_payload = BuiltinToolResult(payload=None)
    envelope_size = serialized_builtin_tool_result_size(
        empty_payload,
        evidence_ref=evidence_ref,
    )
    return byte_cap - (envelope_size - canonical_json_size(None))


def new_evidence_ref() -> str:
    """Allocate a fixed-length opaque evidence reference."""

    return secrets.token_hex(_EVIDENCE_REF_BYTES)


def validate_builtin_spec(spec: BuiltinToolSpec) -> None:
    """Validate provider-facing builtin metadata at runtime construction."""

    if _TOOL_NAME_PATTERN.fullmatch(spec.name) is None:
        raise ValueError(
            f"Builtin tool name {spec.name!r} must match {_TOOL_NAME_PATTERN.pattern}."
        )
    if not isinstance(spec.input_schema, dict) or spec.input_schema.get("type") != "object":
        raise ValueError(f"Builtin tool {spec.name!r} input_schema must describe an object.")
    try:
        canonical_json(spec.input_schema)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Builtin tool {spec.name!r} input_schema must be JSON serializable."
        ) from exc


def validate_builtin_arguments(arguments: object, schema: Mapping[str, Any]) -> list[str]:
    """Validate model arguments against the supported JSON Schema vocabulary."""

    return _schema_errors(arguments, schema, path="$")


def _schema_errors(value: object, schema: Mapping[str, Any], *, path: str) -> list[str]:
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']!r}")

    branches = schema.get("anyOf")
    if isinstance(branches, list) and branches:
        matching = [
            branch
            for branch in branches
            if isinstance(branch, Mapping) and not _schema_errors(value, branch, path=path)
        ]
        if not matching:
            errors.append(f"{path} does not match any allowed schema")
        return errors

    expected = schema.get("type")
    if expected is not None and not _matches_type(value, expected):
        errors.append(f"{path} must be of type {expected!r}")
        return errors

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                errors.append(f"{path}.{name} is required")
        additional = schema.get("additionalProperties", True)
        for name, item in value.items():
            child = properties.get(name)
            if isinstance(child, Mapping):
                errors.extend(_schema_errors(item, child, path=f"{path}.{name}"))
            elif additional is False:
                errors.append(f"{path}.{name} is not allowed")
            elif isinstance(additional, Mapping):
                errors.extend(_schema_errors(item, additional, path=f"{path}.{name}"))

    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(f"{path} must contain at least {schema['minItems']} items")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(f"{path} must contain at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, item_schema, path=f"{path}[{index}]"))

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{path} must contain at least {minimum} characters")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{path} must contain at most {maximum} characters")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path} must match {pattern!r}")

    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int | float) and value < minimum:
            errors.append(f"{path} must be at least {minimum}")
        if isinstance(maximum, int | float) and value > maximum:
            errors.append(f"{path} must be at most {maximum}")
    return errors


def _matches_type(value: object, expected: object) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False
