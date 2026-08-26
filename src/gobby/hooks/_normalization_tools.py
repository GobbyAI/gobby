"""Top-level tool-field normalization orchestration."""

import json as _json
from typing import Any

from gobby.hooks._normalization_canonical import _compact_tool_name, _set_canonical_tool_metadata
from gobby.hooks._normalization_mcp import normalize_mcp_fields
from gobby.hooks._normalization_paths import (
    _normalize_apply_patch_input,
    _normalize_file_change_input,
)
from gobby.hooks._normalization_shell import canonicalize_shell_tool_name
from gobby.hooks.tool_outcomes import normalize_tool_outcome


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

    # function_name -> tool_name  (ACP typed JSON)
    if "function_name" in data and "tool_name" not in data:
        data["tool_name"] = data["function_name"]

    # toolName -> tool_name  (alias normalization)
    if "toolName" in data and "tool_name" not in data:
        data["tool_name"] = data["toolName"]

    if "tool_name" in data:
        data["tool_name"] = canonicalize_shell_tool_name(data["tool_name"])

    # toolArgs -> tool_input  (may be a JSON string)
    if "toolArgs" in data and "tool_input" not in data:
        tool_args = data["toolArgs"]
        if isinstance(tool_args, str):
            try:
                tool_args = _json.loads(tool_args)
            except (ValueError, TypeError):
                pass
        data["tool_input"] = tool_args

    # parameters -> tool_input  (ACP typed JSON)
    if "parameters" in data and "tool_input" not in data:
        data["tool_input"] = data["parameters"]

    # args -> tool_input  (ACP typed JSON fallback)
    if "args" in data and "tool_input" not in data:
        data["tool_input"] = data["args"]

    # Normalize tool_input internal fields (e.g., path -> file_path)
    tool_input = data.get("tool_input")
    tool_name = data.get("tool_name")

    # Aliasing and coercion below mutate this dict in place. Keep the payload
    # the CLI actually sent (first pass wins) so an input rewrite can be
    # returned as a complete replacement instead of echoing normalized keys.
    if isinstance(tool_input, dict):
        data.setdefault("_raw_tool_input", dict(tool_input))

    compact_tool_name = _compact_tool_name(tool_name)
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

    return data


def _detect_tool_error(data: dict[str, Any]) -> None:
    """Normalize structured outcomes and retain the legacy error alias."""
    outcome = normalize_tool_outcome(data)
    if outcome.succeeded is False:
        data.setdefault("is_error", True)
