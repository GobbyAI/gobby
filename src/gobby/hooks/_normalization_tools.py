"""Top-level tool-field normalization orchestration."""

import json as _json
import re as _re
from typing import Any

from gobby.hooks._normalization_canonical import _set_canonical_tool_metadata
from gobby.hooks._normalization_mcp import normalize_mcp_fields
from gobby.hooks._normalization_paths import (
    _normalize_apply_patch_input,
    _normalize_file_change_input,
)
from gobby.hooks._normalization_shell import canonicalize_shell_tool_name, is_shell_tool

# Pattern to detect non-zero exit codes in tool output text.
# Matches: "Exit code: 1", "exit code 127", "Error: Exit code 2", etc.
_EXIT_CODE_RE = _re.compile(r"[Ee]xit.?code[:\s]+(\d+)")


def normalize_tool_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize tool-related fields in hook event data.

    Three-phase normalization:

    1. **Field aliases** - flatten CLI-specific naming into canonical fields
       (``tool_name``, ``tool_input``) using ``setdefault`` semantics so
       adapter-specific pre-processing is never overwritten.
    2. **MCP enrichment** - delegates to :func:`normalize_mcp_fields` for
       ``mcp__`` prefix parsing, ``call_tool`` inner extraction, and
       ``tool_result``/``tool_response`` -> ``tool_output``.
    3. **Error detection** - infers ``is_error`` from tool output content
       for shell tools (Bash) when the adapter didn't set it explicitly.

    This is the primary entry point.  All adapters should call this instead
    of ``normalize_mcp_fields()`` directly.

    Args:
        data: Event data dict (mutated in place).

    Returns:
        The same *data* dict, enriched with normalized fields.
    """
    # Phase 1: field alias normalization

    # function_name -> tool_name  (Gemini)
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

    # parameters -> tool_input  (Gemini)
    if "parameters" in data and "tool_input" not in data:
        data["tool_input"] = data["parameters"]

    # args -> tool_input  (Gemini fallback)
    if "args" in data and "tool_input" not in data:
        data["tool_input"] = data["args"]

    # Normalize tool_input internal fields (e.g., path -> file_path for Gemini)
    tool_input = data.get("tool_input")
    tool_name = data.get("tool_name")

    if isinstance(tool_name, str) and tool_name.lower() == "apply_patch":
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

    # mcp_context {} -> mcp_server / mcp_tool  (Gemini MCP)
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

    # Phase 3: infer is_error from tool output for shell tools
    _detect_tool_error(data)

    return data


def _detect_tool_error(data: dict[str, Any]) -> None:
    """Infer ``is_error`` from tool output for shell tools (Phase 3).

    Some adapters set ``is_error`` explicitly via
    ``exit_code`` or ``resultType``.  Claude Code and Gemini do not - they
    only provide the tool output text.  For shell tools (Bash), we parse the
    output for non-zero exit code patterns and set ``is_error = True``.

    Skips if ``is_error`` is already set to avoid overriding adapter-specific
    detection.
    """
    if "is_error" in data:
        return

    tool_name = data.get("tool_name", "")
    if not is_shell_tool(tool_name):
        return

    # Check tool_output (normalized) or fall back to tool_result (raw)
    output = data.get("tool_output") or data.get("tool_result") or ""
    if not isinstance(output, str):
        return

    match = _EXIT_CODE_RE.search(output)
    if match and match.group(1) != "0":
        data["is_error"] = True
