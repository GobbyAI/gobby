"""Top-level tool-field normalization orchestration."""

from typing import Any

from gobby.hooks._normalization_canonical import _compact_tool_name, _set_canonical_tool_metadata
from gobby.hooks._normalization_mcp import normalize_mcp_fields
from gobby.hooks._normalization_paths import (
    _normalize_apply_patch_input,
    _normalize_file_change_input,
)
from gobby.hooks._normalization_shell import canonicalize_shell_tool_name
from gobby.hooks._normalization_tool_input import (
    TOOL_INPUT_ERROR_FIELD,
    decode_string_tool_input,
    is_non_object_tool_input,
    mark_tool_input_unavailable,
    tool_input_error,
    tool_input_source,
)
from gobby.hooks.code_navigation_recovery import annotate_navigation_outcome
from gobby.hooks.tool_outcomes import normalize_tool_outcome

_TOOL_INPUT_FIELD_ALIASES = (
    ("cmd", "command"),
    ("CommandLine", "command"),
    ("Cwd", "cwd"),
    ("TargetFile", "file_path"),
    ("AbsolutePath", "file_path"),
    ("DirectoryPath", "file_path"),
)


def normalize_tool_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize tool-related fields in hook event data.

    Three-phase normalization:

    1. **Field aliases** - flatten CLI-specific naming into canonical fields
       (``tool_name``, ``tool_input``) using ``setdefault`` semantics so
       adapter-specific pre-processing is never overwritten.
    2. **MCP enrichment** - delegates to :func:`normalize_mcp_fields` for
       ``mcp__`` prefix parsing, ``call_tool`` inner extraction, and
       ``tool_result``/``tool_response`` -> ``tool_output``.
    3. **Outcome normalization** - reduces structured provider signals to the
       canonical succeeded/failed/unknown tool outcome.

    This is the primary entry point.  All adapters should call this instead
    of ``normalize_mcp_fields()`` directly.

    Args:
        data: Event data dict (mutated in place).

    Returns:
        The same *data* dict, enriched with normalized fields.
    """
    # Phase 1: field alias normalization

    # A marker survives only on a repeat pass over input it already replaced;
    # any sender-supplied marker is re-derived or dropped below.
    prior_error = tool_input_error(data)
    data.pop(TOOL_INPUT_ERROR_FIELD, None)
    source = tool_input_source(data)

    # function_name -> tool_name  (ACP typed JSON)
    if "function_name" in data and "tool_name" not in data:
        data["tool_name"] = data["function_name"]

    # toolName -> tool_name  (alias normalization)
    if "toolName" in data and "tool_name" not in data:
        data["tool_name"] = data["toolName"]

    if "tool_name" in data:
        data["tool_name"] = canonicalize_shell_tool_name(data["tool_name"])

    # toolArgs -> tool_input  (may be a JSON string, decoded below)
    if "toolArgs" in data and "tool_input" not in data:
        data["tool_input"] = data["toolArgs"]

    # parameters -> tool_input  (ACP typed JSON)
    if "parameters" in data and "tool_input" not in data:
        data["tool_input"] = data["parameters"]

    # args -> tool_input  (ACP typed JSON fallback)
    if "args" in data and "tool_input" not in data:
        data["tool_input"] = data["args"]

    # apply_patch input is freeform patch text, never JSON.
    compact_tool_name = _compact_tool_name(data.get("tool_name"))
    decoded_string = compact_tool_name != "applypatch" and decode_string_tool_input(data, source)
    # A list, number, or bool sent directly; Write and apply_patch may still recover it below.
    sent_non_object = (
        not decoded_string
        and "tool_name" in data
        and is_non_object_tool_input(data.get("tool_input"))
    )

    # Normalize tool_input internal fields (e.g., path -> file_path)
    tool_input = data.get("tool_input")
    tool_name = data.get("tool_name")

    # Aliasing and coercion below mutate this dict in place. Keep the payload
    # the CLI actually sent (first pass wins) so an input rewrite can be
    # returned as a complete replacement instead of echoing normalized keys.
    if isinstance(tool_input, dict):
        data.setdefault("_raw_tool_input", dict(tool_input))
        for provider_name, canonical_name in _TOOL_INPUT_FIELD_ALIASES:
            if provider_name in tool_input and canonical_name not in tool_input:
                tool_input[canonical_name] = tool_input[provider_name]

    if compact_tool_name == "applypatch":
        data.setdefault("_original_tool_name", tool_name)
        data["tool_name"] = "Write"
        tool_input = _normalize_apply_patch_input(tool_input)
        data["tool_input"] = tool_input
    elif data.get("tool_name") == "Write":
        normalized_input = _normalize_file_change_input(tool_input)
        if normalized_input is not tool_input:
            data["tool_input"] = normalized_input
            tool_input = normalized_input

    if decoded_string and "tool_input" in data and not isinstance(tool_input, dict):
        mark_tool_input_unavailable(data, source, "non_object_json")
    elif sent_non_object and (not isinstance(tool_input, dict) or not tool_input):
        mark_tool_input_unavailable(data, source, "non_object")
    elif prior_error is not None and "tool_input" not in data:
        data.setdefault(TOOL_INPUT_ERROR_FIELD, prior_error)

    if isinstance(tool_input, dict):
        if "path" in tool_input and "file_path" not in tool_input:
            tool_input["file_path"] = tool_input["path"]

    # mcp_context {} -> mcp_server / mcp_tool
    mcp_context = data.get("mcp_context")
    if mcp_context and isinstance(mcp_context, dict):
        server = mcp_context.get("server_name")
        if server and "mcp_server" not in data:
            data["mcp_server"] = server
        tool = mcp_context.get("tool_name")
        if tool and "mcp_tool" not in data:
            data["mcp_tool"] = tool

    # Phase 2: MCP prefix/inner extraction + output aliases
    normalize_mcp_fields(data)

    # Native command fields can retain a shell wrapper or a stale command.
    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict) and "command" in tool_input:
        data["command"] = tool_input["command"]

    # Phase 2.5: infer canonical read/search/write semantics
    _set_canonical_tool_metadata(data)

    # Phase 3: normalize machine-readable outcomes only for tool-shaped data.
    # Adapters also use this helper for session and turn events, where adding an
    # unknown tool outcome would pollute otherwise untouched event payloads.
    if any(
        field in data
        for field in (
            "tool_name",
            "toolName",
            "function_name",
            "tool_output",
            "tool_result",
            "tool_response",
        )
    ):
        _detect_tool_error(data)
        annotate_navigation_outcome(data)

    return data


def _detect_tool_error(data: dict[str, Any]) -> None:
    """Normalize structured outcomes and retain the legacy error alias."""
    outcome = normalize_tool_outcome(data)
    if outcome.succeeded is False:
        data.setdefault("is_error", True)
