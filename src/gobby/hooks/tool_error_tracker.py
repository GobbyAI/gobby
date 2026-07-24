"""Bounded unresolved-tool-error state shared by hook and proxy transports."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

MAX_IDENTITY_COMPONENT_CHARS = 130
MAX_ERROR_CHARS = 300
MAX_OPEN_TOOL_ERRORS = 10
MAX_TOOL_ERROR_COUNT = 999_999

_DIGEST_CHARS = 8
_IDENTITY_SEPARATOR = "…#"
_COMMAND_PREFIX_CHARS = 80
_WRAPPER_TOOL_NAMES = frozenset(
    {
        "call_tool",
        "gobby.call_tool",
        "mcp_gobby_call_tool",
        "mcp__gobby__call_tool",
    }
)
_EXECUTE_TOOL_NAMES = frozenset(
    {
        "bash",
        "exec",
        "exec_command",
        "execute",
        "execute_command",
        "run_command",
        "shell",
    }
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


class ToolEventLike(Protocol):
    data: dict[str, Any]
    metadata: dict[str, Any]
    timestamp: datetime


class ToolErrorVariableManager(Protocol):
    def upsert_open_tool_error(
        self,
        session_id: str,
        tool: str,
        target_key: str,
        error: str,
        *,
        occurred_at: datetime,
    ) -> None: ...

    def resolve_open_tool_errors(
        self,
        session_id: str,
        tool: str,
        target_key: str,
    ) -> None: ...


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def sanitize_record_text(value: object) -> str:
    """Collapse structure-changing controls and escape leading Markdown tokens."""
    text = _CONTROL_CHARACTERS.sub(" ", str(value)).strip()
    if text.startswith("#"):
        leading_hashes = len(text) - len(text.lstrip("#"))
        text = ("\\#" * leading_hashes) + text[leading_hashes:]
    elif text.startswith("```") or text.startswith("~~~"):
        text = "\\" + text
    return text


def render_bounded_identity(value: str) -> str:
    """Render an identity component under one exact total-length authority."""
    if len(value) <= MAX_IDENTITY_COMPONENT_CHARS:
        return value
    prefix_chars = MAX_IDENTITY_COMPONENT_CHARS - len(_IDENTITY_SEPARATOR) - _DIGEST_CHARS
    return f"{value[:prefix_chars]}{_IDENTITY_SEPARATOR}{_digest(value)}"


def _bounded_identity(value: object) -> str:
    return render_bounded_identity(sanitize_record_text(value))


def _normalize_hash_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_hash_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_hash_value(item) for item in value]
    if isinstance(value, set | frozenset):
        normalized = [_normalize_hash_value(item) for item in value]
        return sorted(normalized, key=lambda item: _canonical_json(item))
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return f"{type(value).__module__}.{type(value).__qualname__}:{value}"


def _canonical_json(value: object) -> str:
    return json.dumps(
        _normalize_hash_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _composed_target(readable: str, digest_basis: str) -> str:
    return _bounded_identity(f"{readable}#{_digest(digest_basis)}")


def extract_target_key(data: Mapping[str, Any], arguments: Mapping[str, Any]) -> str:
    """Extract one bounded, exact-match target identity."""
    raw_paths = data.get("canonical_file_paths")
    paths = (
        sorted(path for path in raw_paths if isinstance(path, str))
        if isinstance(raw_paths, Sequence) and not isinstance(raw_paths, str | bytes)
        else []
    )
    if not paths:
        for key in ("file_path", "path", "target_file"):
            path = arguments.get(key)
            if isinstance(path, str) and path:
                paths = [path]
                break
    if not paths:
        for key in ("file_paths", "paths"):
            values = arguments.get(key)
            if isinstance(values, Sequence) and not isinstance(values, str | bytes):
                paths = sorted(path for path in values if isinstance(path, str))
                if paths:
                    break
    if paths:
        readable = paths[0] + (f"+{len(paths) - 1}" if len(paths) > 1 else "")
        return _composed_target(readable, _canonical_json(paths))

    tool_name = str(data.get("tool_name", "")).lower()
    canonical_kind = str(data.get("canonical_tool_kind", "")).lower()
    command = next(
        (
            arguments[key]
            for key in ("command", "cmd", "script")
            if isinstance(arguments.get(key), str)
        ),
        None,
    )
    if isinstance(command, str) and (
        canonical_kind == "execute" or tool_name in _EXECUTE_TOOL_NAMES
    ):
        return _composed_target(command[:_COMMAND_PREFIX_CHARS], command)

    return f"args:{_digest(_canonical_json(arguments))}"


def normalize_tool_identity(event: ToolEventLike) -> tuple[str, dict[str, Any]]:
    """Return the real routed tool identity and its arguments."""
    data = event.data
    tool_input = data.get("tool_input")
    input_mapping = tool_input if isinstance(tool_input, Mapping) else {}
    mcp_server = data.get("mcp_server")
    mcp_tool = data.get("mcp_tool")
    if isinstance(mcp_server, str) and isinstance(mcp_tool, str):
        nested = input_mapping.get("arguments")
        arguments = dict(nested) if isinstance(nested, Mapping) else {}
        return f"{mcp_server}/{mcp_tool}", arguments
    tool_name = data.get("tool_name")
    return str(tool_name or "unknown"), dict(input_mapping)


def is_wrapper_echo_event(event: ToolEventLike) -> bool:
    """Return whether a CLI after-event echoes the public proxy wrapper."""
    tool_name = event.data.get("tool_name")
    return isinstance(tool_name, str) and tool_name.lower() in _WRAPPER_TOOL_NAMES


def _mapping_error_text(value: Mapping[str, Any]) -> str | None:
    for key in ("error", "errors"):
        if key not in value:
            continue
        nested = value[key]
        if isinstance(nested, str) and nested:
            return nested
        if isinstance(nested, Mapping):
            found = _mapping_error_text(nested)
            if found:
                return found
            message = nested.get("message")
            if isinstance(message, str) and message:
                return message

    if value.get("isError") is True or value.get("is_error") is True:
        content = value.get("content")
        if isinstance(content, Sequence) and not isinstance(content, str | bytes):
            texts = [
                str(item["text"])
                for item in content
                if isinstance(item, Mapping) and isinstance(item.get("text"), str)
            ]
            if texts:
                return "\n".join(texts)

    for key in ("tool_output", "tool_response", "tool_result", "structuredContent"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = _mapping_error_text(nested)
            if found:
                return found
        elif isinstance(nested, str) and nested:
            return nested
    return None


def _serializable_result(source: object) -> object:
    model_dump = getattr(source, "model_dump", None)
    if callable(model_dump):
        return model_dump(by_alias=True)
    if isinstance(source, Mapping | Sequence | str | int | float | bool) or source is None:
        return source
    return {"type": f"{type(source).__module__}.{type(source).__qualname__}"}


def extract_error_snippet(source: object) -> str:
    """Extract one canonical, bounded, nonempty error message."""
    serializable = _serializable_result(source)
    if isinstance(source, str):
        text = source
    elif isinstance(serializable, Mapping):
        text = _mapping_error_text(serializable) or _canonical_json(serializable)
    else:
        text = _canonical_json(serializable)
    sanitized = sanitize_record_text(text)
    if not sanitized:
        sanitized = '{"error":"unknown tool failure"}'
    return sanitized[:MAX_ERROR_CHARS]


def _canonical_timestamp(value: object) -> tuple[str, datetime] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    normalized = parsed.astimezone(UTC)
    return normalized.isoformat(timespec="seconds"), normalized


def _bounded_count(value: object) -> int:
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str | float):
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return 1
    else:
        return 1
    return min(MAX_TOOL_ERROR_COUNT, max(1, parsed))


def normalize_open_tool_error_records(raw: object) -> list[dict[str, Any]]:
    """Normalize foreign or stale stored state before any read-side use."""
    if not isinstance(raw, list):
        return []
    normalized: list[tuple[datetime, dict[str, Any]]] = []
    required = {"tool", "target_key", "error", "first_at", "last_at", "count"}
    for value in raw:
        if not isinstance(value, Mapping) or not required.issubset(value):
            continue
        first = _canonical_timestamp(value["first_at"])
        last = _canonical_timestamp(value["last_at"])
        if first is None or last is None:
            continue
        record = {
            "tool": _bounded_identity(value["tool"]),
            "target_key": _bounded_identity(value["target_key"]),
            "error": sanitize_record_text(value["error"])[:MAX_ERROR_CHARS],
            "first_at": first[0],
            "last_at": last[0],
            "count": _bounded_count(value["count"]),
        }
        normalized.append((last[1], record))
    normalized.sort(key=lambda pair: pair[0])
    return [record for _, record in normalized[-MAX_OPEN_TOOL_ERRORS:]]


def track_tool_outcome(
    sv_mgr: ToolErrorVariableManager,
    session_id: str,
    event: ToolEventLike,
) -> None:
    """Track a native CLI after-tool outcome."""
    tool, arguments = normalize_tool_identity(event)
    data = event.data
    target_key = extract_target_key(data, arguments)
    if event.metadata.get("is_failure") is True:
        sv_mgr.upsert_open_tool_error(
            session_id,
            _bounded_identity(tool),
            target_key,
            extract_error_snippet(data),
            occurred_at=event.timestamp,
        )
        return
    sv_mgr.resolve_open_tool_errors(session_id, _bounded_identity(tool), target_key)


def _proxy_identity(
    identity: tuple[str, str, Mapping[str, Any]],
) -> tuple[str, str]:
    server_name, tool_name, arguments = identity
    tool = _bounded_identity(f"{server_name}/{tool_name}")
    target_key = extract_target_key({"tool_name": tool_name}, arguments)
    return tool, target_key


def track_proxy_outcome(
    sv_mgr: ToolErrorVariableManager,
    session_id: str | None,
    caller_identity: tuple[str, str, Mapping[str, Any]],
    final_identity: tuple[str, str, Mapping[str, Any]],
    result: object,
    outcome_class: str,
) -> None:
    """Track one proxy-owned outcome according to its structural return class."""
    if session_id is None or outcome_class in {"policy_denied", "invalid_call"}:
        return
    caller_tool, caller_target = _proxy_identity(caller_identity)
    final_tool, final_target = _proxy_identity(final_identity)
    occurred_at = datetime.now(UTC)
    if outcome_class == "failed_pre_dispatch":
        sv_mgr.upsert_open_tool_error(
            session_id,
            caller_tool,
            caller_target,
            extract_error_snippet(result),
            occurred_at=occurred_at,
        )
        return
    if outcome_class != "executed":
        raise ValueError(f"Unknown proxy outcome class: {outcome_class}")

    from gobby.hooks.tool_outcomes import classify_raw_tool_result

    succeeded = classify_raw_tool_result(result).succeeded
    identities_differ = (caller_tool, caller_target) != (final_tool, final_target)
    if succeeded is False:
        if identities_differ:
            sv_mgr.resolve_open_tool_errors(session_id, caller_tool, caller_target)
        sv_mgr.upsert_open_tool_error(
            session_id,
            final_tool,
            final_target,
            extract_error_snippet(result),
            occurred_at=occurred_at,
        )
        return
    sv_mgr.resolve_open_tool_errors(session_id, caller_tool, caller_target)
    if identities_differ:
        sv_mgr.resolve_open_tool_errors(session_id, final_tool, final_target)
