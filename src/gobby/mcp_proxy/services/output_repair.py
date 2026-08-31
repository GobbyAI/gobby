"""Repair OpenAPI-backed tool results against the spec's response schema.

``awslabs.openapi-mcp-server`` validates outputs inside its own process
(``VALIDATE_OUTPUT``) and rejects any response that drifts from the spec. The
openapi template's ``output_validation: repair`` switches that check off and
the proxy applies the response schema itself: a null in a non-nullable field
is dropped or replaced by a typed empty value, parseable scalars are coerced
to the declared type, and every change is reported to the caller under
``schema_deviations``. Tool naming and response-schema selection mirror
FastMCP, which generates the upstream tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from mcp.types import CallToolResult, TextContent

from gobby.mcp_proxy.services.server_resolution import fallback_project_id, resolve_server

logger = logging.getLogger(__name__)

OPENAPI_TEMPLATE = "openapi"
OUTPUT_VALIDATION_PARAM = "output_validation"
NULL_POLICY_PARAM = "repair_null_policy"
REPAIR_MODE = "repair"
NULL_POLICY_DROP = "drop"
NULL_POLICY_EMPTY = "empty"
DEVIATIONS_KEY = "schema_deviations"
DEVIATIONS_TRUNCATED_KEY = "schema_deviations_truncated"
MAX_DEVIATIONS = 50

_TOOL_NAME_LIMIT = 56
_SUCCESS_CODES = ("200", "201", "202", "204")
_JSON_CONTENT_TYPES = (
    "application/json",
    "application/vnd.api+json",
    "application/hal+json",
    "application/ld+json",
    "text/json",
)
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_TYPED_EMPTY: dict[str, Any] = {
    "string": "",
    "number": 0,
    "integer": 0,
    "boolean": False,
    "array": [],
    "object": {},
}
_SPEC_FETCH_TIMEOUT_SECONDS = 30.0
_FAILED_LOAD_RETRY_SECONDS = 300.0
_MAX_REF_DEPTH = 32
_DROP = object()


@dataclass(frozen=True)
class ToolOutputSchema:
    """Response schema for one generated tool."""

    schema: dict[str, Any]
    wrapped: bool
    """Non-object responses are exposed under ``{"result": ...}`` by FastMCP."""


@dataclass(frozen=True)
class ResponseSchemaIndex:
    """Tool name -> response schema, plus the document for ``$ref`` lookups."""

    tools: dict[str, ToolOutputSchema]
    document: dict[str, Any]


@dataclass
class _RepairContext:
    document: dict[str, Any]
    null_policy: str
    deviations: list[dict[str, str]] = field(default_factory=list)
    truncated: int = 0

    def record(self, path: str, expected: str, actual: str, action: str) -> None:
        if len(self.deviations) < MAX_DEVIATIONS:
            self.deviations.append(
                {"path": path, "expected": expected, "actual": actual, "action": action}
            )
        else:
            self.truncated += 1


@dataclass
class _CacheEntry:
    index: ResponseSchemaIndex | None
    loaded_at: float


_INDEX_CACHE: dict[str, _CacheEntry] = {}


# ---------------------------------------------------------------------------
# Spec index
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    text = re.sub(r"[\s\-\.]+", "_", text)
    text = re.sub(r"[^a-zA-Z0-9_]", "", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def _tool_base_name(operation: dict[str, Any], method: str, path: str) -> str:
    operation_id = operation.get("operationId")
    if isinstance(operation_id, str) and operation_id:
        return operation_id.split("__")[0]
    summary = operation.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    return f"{method}_{path}"


def _lookup_ref(document: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    node: Any = document
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, dict) else None


def _deref(schema: Any, document: dict[str, Any]) -> dict[str, Any]:
    depth = 0
    while (
        isinstance(schema, dict) and isinstance(schema.get("$ref"), str) and depth < _MAX_REF_DEPTH
    ):
        target = _lookup_ref(document, schema["$ref"])
        if target is None:
            break
        schema = target
        depth += 1
    return schema if isinstance(schema, dict) else {}


def _response_schema(responses: Any, document: dict[str, Any]) -> ToolOutputSchema | None:
    if not isinstance(responses, dict):
        return None
    response: Any = None
    for code in _SUCCESS_CODES:
        if code in responses:
            response = responses[code]
            break
    else:
        for code, candidate in responses.items():
            if str(code).startswith("2"):
                response = candidate
                break
    response = _deref(response, document)
    content = response.get("content")
    if not isinstance(content, dict) or not content:
        return None
    media: Any = None
    for content_type in _JSON_CONTENT_TYPES:
        if content_type in content:
            media = content[content_type]
            break
    else:
        media = next(iter(content.values()))
    if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
        return None
    schema: dict[str, Any] = media["schema"]
    wrapped = _primary_type(_deref(schema, document)) != "object"
    return ToolOutputSchema(schema=schema, wrapped=wrapped)


def build_schema_index(document: dict[str, Any]) -> ResponseSchemaIndex:
    """Map FastMCP tool names to their response schemas."""
    tools: dict[str, ToolOutputSchema] = {}
    counts: dict[str, int] = {}
    paths = document.get("paths")
    for path, item in paths.items() if isinstance(paths, dict) else ():
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            name = _slugify(_tool_base_name(operation, method, str(path)))[:_TOOL_NAME_LIMIT]
            seen = counts.get(name, 0) + 1
            counts[name] = seen
            if seen > 1:
                name = f"{name}_{seen}"
            output = _response_schema(operation.get("responses"), document)
            if output is not None:
                tools[name] = output
    return ResponseSchemaIndex(tools=tools, document=document)


def _load_spec_document(source: str) -> dict[str, Any]:
    if source.startswith(("http://", "https://")):
        response = httpx.get(source, timeout=_SPEC_FETCH_TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
        text = response.text
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except ValueError:
        document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError(f"OpenAPI document at {source} is not a mapping")
    return document


async def get_schema_index(source: str) -> ResponseSchemaIndex | None:
    """Load and cache the response-schema index for a spec path or URL."""
    now = time.monotonic()
    entry = _INDEX_CACHE.get(source)
    if entry is not None and (
        entry.index is not None or now - entry.loaded_at < _FAILED_LOAD_RETRY_SECONDS
    ):
        return entry.index
    try:
        document = await asyncio.to_thread(_load_spec_document, source)
        index = build_schema_index(document)
    except Exception as exc:
        logger.warning("Output repair cannot load OpenAPI spec %s: %s", source, exc)
        _INDEX_CACHE[source] = _CacheEntry(index=None, loaded_at=now)
        return None
    _INDEX_CACHE[source] = _CacheEntry(index=index, loaded_at=now)
    return index


def clear_schema_index_cache() -> None:
    _INDEX_CACHE.clear()


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _primary_type(schema: dict[str, Any]) -> str | None:
    declared = schema.get("type")
    if isinstance(declared, list):
        for candidate in declared:
            if candidate != "null":
                return str(candidate)
        return None
    if isinstance(declared, str):
        return None if declared == "null" else declared
    if "properties" in schema or "additionalProperties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


def _union_branches(schema: dict[str, Any]) -> list[Any]:
    branches = schema.get("anyOf") or schema.get("oneOf")
    return list(branches) if isinstance(branches, list) else []


def _nullable(schema: dict[str, Any], document: dict[str, Any], depth: int = 0) -> bool:
    if schema.get("nullable") is True:
        return True
    declared = schema.get("type")
    if declared == "null" or (isinstance(declared, list) and "null" in declared):
        return True
    enum = schema.get("enum")
    if isinstance(enum, list) and None in enum:
        return True
    if depth >= 8:
        return False
    return any(
        _nullable(_deref(branch, document), document, depth + 1)
        for branch in _union_branches(schema)
    )


def _typed_empty(expected: str) -> Any:
    empty = _TYPED_EMPTY[expected]
    return type(empty)() if isinstance(empty, list | dict) else empty


def _parse_json_text(value: Any, expected: type) -> Any:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, expected) else None


def _coerce_scalar(value: Any, expected: str) -> tuple[Any, bool]:
    """Return ``(coerced, changed)``; ``changed`` is False when nothing applies."""
    if expected == "string":
        if isinstance(value, bool):
            return ("true" if value else "false"), True
        if isinstance(value, int | float):
            return str(value), True
        return value, False
    if isinstance(value, bool):
        return value, False
    if expected == "integer":
        if isinstance(value, float) and value.is_integer():
            return int(value), True
        if isinstance(value, str):
            try:
                return int(value.strip()), True
            except ValueError:
                return value, False
        return value, False
    if expected == "number":
        if isinstance(value, str):
            text = value.strip()
            try:
                return (int(text) if re.fullmatch(r"[+-]?\d+", text) else float(text)), True
            except ValueError:
                return value, False
        return value, False
    if expected == "boolean":
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True, True
            if lowered in {"false", "0", "no"}:
                return False, True
            return value, False
        if isinstance(value, int) and value in (0, 1):
            return bool(value), True
        return value, False
    return value, False


def _select_union_branch(
    value: Any, schema: dict[str, Any], ctx: _RepairContext
) -> dict[str, Any] | None:
    branches = [_deref(branch, ctx.document) for branch in _union_branches(schema)]
    if not branches:
        return None
    actual = _json_type(value)
    for branch in branches:
        if _primary_type(branch) == actual:
            return branch
    for branch in branches:
        if _primary_type(branch) is not None:
            return branch
    return None


def _repair(value: Any, schema: Any, ctx: _RepairContext, path: str) -> Any:
    schema = _deref(schema, ctx.document)
    if not schema:
        return value
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            value = _repair(value, branch, ctx, path)
            if value is _DROP:
                return _DROP
    if value is None:
        if _nullable(schema, ctx.document):
            return None
        expected = _primary_type(schema)
        if expected is None:
            branch = _select_union_branch(value, schema, ctx)
            expected = _primary_type(branch) if branch is not None else None
        if expected is None:
            return None
        if ctx.null_policy == NULL_POLICY_EMPTY and expected in _TYPED_EMPTY:
            ctx.record(path, expected, "null", "replaced_with_empty")
            return _typed_empty(expected)
        ctx.record(path, expected, "null", "dropped")
        return _DROP
    expected = _primary_type(schema)
    if expected is None:
        branch = _select_union_branch(value, schema, ctx)
        return value if branch is None else _repair(value, branch, ctx, path)
    actual = _json_type(value)
    if expected == "object":
        if isinstance(value, dict):
            return _repair_object(value, schema, ctx, path)
        parsed = _parse_json_text(value, dict)
        if parsed is None:
            ctx.record(path, expected, actual, "unchanged")
            return value
        ctx.record(path, expected, actual, "parsed_json")
        return _repair_object(parsed, schema, ctx, path)
    if expected == "array":
        if isinstance(value, list):
            return _repair_array(value, schema, ctx, path)
        parsed = _parse_json_text(value, list)
        if parsed is None:
            ctx.record(path, expected, actual, "unchanged")
            return value
        ctx.record(path, expected, actual, "parsed_json")
        return _repair_array(parsed, schema, ctx, path)
    if actual == expected or (expected == "number" and actual == "integer"):
        return value
    coerced, changed = _coerce_scalar(value, expected)
    ctx.record(path, expected, actual, "coerced" if changed else "unchanged")
    return coerced


def _repair_object(
    value: dict[str, Any], schema: dict[str, Any], ctx: _RepairContext, path: str
) -> dict[str, Any]:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    additional = schema.get("additionalProperties")
    for key in list(value):
        property_schema = properties.get(key)
        if property_schema is None and isinstance(additional, dict):
            property_schema = additional
        if property_schema is None:
            continue
        repaired = _repair(value[key], property_schema, ctx, f"{path}.{key}")
        if repaired is _DROP:
            del value[key]
        else:
            value[key] = repaired
    return value


def _repair_array(
    value: list[Any], schema: dict[str, Any], ctx: _RepairContext, path: str
) -> list[Any]:
    items = schema.get("items")
    if not isinstance(items, dict):
        return value
    kept: list[Any] = []
    for index, item in enumerate(value):
        repaired = _repair(item, items, ctx, f"{path}[{index}]")
        if repaired is not _DROP:
            kept.append(repaired)
    value[:] = kept
    return value


def repair_value(
    value: Any, schema: dict[str, Any], *, document: dict[str, Any] | None = None, null_policy: str
) -> tuple[Any, list[dict[str, str]]]:
    """Repair ``value`` against ``schema``; return the value and the deviations."""
    ctx = _RepairContext(document=document or {}, null_policy=null_policy)
    repaired = _repair(value, schema, ctx, "$")
    return (value if repaired is _DROP else repaired), ctx.deviations


def _repair_structured(
    structured: dict[str, Any], output: ToolOutputSchema, ctx: _RepairContext
) -> dict[str, Any]:
    resolved = _deref(output.schema, ctx.document)
    properties = resolved.get("properties")
    declared = set(properties) if isinstance(properties, dict) else set()
    if set(structured) == {"result"} and (output.wrapped or "result" not in declared):
        repaired = _repair(structured["result"], output.schema, ctx, "$")
        if repaired is not _DROP:
            structured["result"] = repaired
        return structured
    repaired = _repair(structured, output.schema, ctx, "$")
    return structured if repaired is _DROP else repaired


def repair_call_result(
    result: CallToolResult,
    output: ToolOutputSchema,
    index: ResponseSchemaIndex,
    *,
    null_policy: str,
) -> tuple[CallToolResult, list[dict[str, str]]]:
    """Repair a downstream result's structured and text payloads in place."""
    ctx = _RepairContext(document=index.document, null_policy=null_policy)
    structured = result.structured_content
    if isinstance(structured, dict):
        structured = _repair_structured(structured, output, ctx)
    content = list(result.content)
    for position, item in enumerate(content):
        if not isinstance(item, TextContent):
            continue
        try:
            payload = json.loads(item.text)
        except ValueError:
            continue
        text_ctx = (
            ctx if not isinstance(structured, dict) else _RepairContext(ctx.document, null_policy)
        )
        repaired = _repair(payload, output.schema, text_ctx, "$")
        if text_ctx.deviations and repaired is not _DROP:
            content[position] = item.model_copy(update={"text": json.dumps(repaired)})
    if not ctx.deviations:
        return result, []
    if isinstance(structured, dict):
        structured[DEVIATIONS_KEY] = ctx.deviations
        if ctx.truncated:
            structured[DEVIATIONS_TRUNCATED_KEY] = ctx.truncated
    else:
        report: dict[str, Any] = {DEVIATIONS_KEY: ctx.deviations}
        if ctx.truncated:
            report[DEVIATIONS_TRUNCATED_KEY] = ctx.truncated
        content.append(TextContent(type="text", text=json.dumps(report)))
    repaired_result = result.model_copy(
        update={"content": content, "structured_content": structured}
    )
    return repaired_result, ctx.deviations


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------


async def _repair_if_configured(
    *,
    service: Any,
    server_name: str,
    tool_name: str,
    result: CallToolResult,
    project_id: str | None,
    dispatch_id: str | None,
) -> CallToolResult:
    scope = project_id or fallback_project_id(service)
    config = None
    if dispatch_id:
        config = resolve_server(service, server_id=dispatch_id, project_id=scope)
    if config is None:
        config = resolve_server(service, server_name, project_id=scope)
    if config is None or config.template != OPENAPI_TEMPLATE:
        return result
    values = config.template_values or {}
    if values.get(OUTPUT_VALIDATION_PARAM) != REPAIR_MODE:
        return result
    source = values.get("spec_path") or values.get("spec_url")
    if not isinstance(source, str) or not source:
        return result
    index = await get_schema_index(source)
    if index is None:
        return result
    output = index.tools.get(tool_name)
    if output is None:
        return result
    null_policy = str(values.get(NULL_POLICY_PARAM) or NULL_POLICY_DROP)
    repaired, deviations = repair_call_result(result, output, index, null_policy=null_policy)
    if deviations:
        logger.info(
            "Repaired %d schema deviation(s) in %s/%s (null_policy=%s); first: %s",
            len(deviations),
            server_name,
            tool_name,
            null_policy,
            deviations[0],
        )
    return repaired


async def maybe_repair_output(
    *,
    service: Any,
    server_name: str,
    tool_name: str,
    result: Any,
    project_id: str | None,
    dispatch_id: str | None = None,
) -> Any:
    """Repair an openapi instance's result when its ``output_validation`` is ``repair``.

    Any failure falls open: the original result is returned and the cause logged.
    """
    if not isinstance(result, CallToolResult) or result.is_error:
        return result
    try:
        return await _repair_if_configured(
            service=service,
            server_name=server_name,
            tool_name=tool_name,
            result=result,
            project_id=project_id,
            dispatch_id=dispatch_id,
        )
    except Exception as exc:
        logger.warning("Output repair skipped for %s/%s: %s", server_name, tool_name, exc)
        return result
