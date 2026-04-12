"""Shared tool field normalization for hook events.

Provides two-phase normalization so every CLI adapter produces consistent
canonical fields (``tool_name``, ``tool_input``, ``tool_output``,
``mcp_server``, ``mcp_tool``, ``is_error``) and rules match uniformly.

Phase 1 (``normalize_tool_fields``): flatten CLI-specific field aliases
Phase 2 (``normalize_mcp_fields``):  MCP prefix/inner extraction + output aliases

Used by all adapters and the web-chat path.
"""

import json as _json
import re as _re
from typing import Any

# Tools that run shell commands. ``Bash`` is the canonical runtime name, but
# several adapters and transcripts use shell aliases that should behave the same.
_SHELL_TOOLS = frozenset(
    {
        "Bash",
        "bash",
        "shell",
        "run_command",
        "run_shell_command",
        "RunShellCommand",
        "ShellTool",
        "commandExecution",
        "exec_command",
    }
)

# Pattern to detect non-zero exit codes in tool output text.
# Matches: "Exit code: 1", "exit code 127", "Error: Exit code 2", etc.
_EXIT_CODE_RE = _re.compile(r"[Ee]xit.?code[:\s]+(\d+)")


def is_shell_tool(tool_name: Any) -> bool:
    """Return True when ``tool_name`` represents shell command execution."""
    return isinstance(tool_name, str) and tool_name in _SHELL_TOOLS


def canonicalize_shell_tool_name(tool_name: Any) -> Any:
    """Normalize shell aliases to the canonical ``Bash`` tool name."""
    if is_shell_tool(tool_name):
        return "Bash"
    return tool_name


def normalize_tool_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize tool-related fields in hook event data.

    Three-phase normalization:

    1. **Field aliases** – flatten CLI-specific naming into canonical fields
       (``tool_name``, ``tool_input``) using ``setdefault`` semantics so
       adapter-specific pre-processing is never overwritten.
    2. **MCP enrichment** – delegates to :func:`normalize_mcp_fields` for
       ``mcp__`` prefix parsing, ``call_tool`` inner extraction, and
       ``tool_result``/``tool_response`` → ``tool_output``.
    3. **Error detection** – infers ``is_error`` from tool output content
       for shell tools (Bash) when the adapter didn't set it explicitly.

    This is the primary entry point.  All adapters should call this instead
    of ``normalize_mcp_fields()`` directly.

    Args:
        data: Event data dict (mutated in place).

    Returns:
        The same *data* dict, enriched with normalized fields.
    """
    # ── Phase 1: field alias normalization ──────────────────────────────

    # function_name → tool_name  (Gemini)
    if "function_name" in data and "tool_name" not in data:
        data["tool_name"] = data["function_name"]

    # toolName → tool_name  (alias normalization)
    if "toolName" in data and "tool_name" not in data:
        data["tool_name"] = data["toolName"]

    if "tool_name" in data:
        data["tool_name"] = canonicalize_shell_tool_name(data["tool_name"])

    # toolArgs → tool_input  (may be a JSON string)
    if "toolArgs" in data and "tool_input" not in data:
        tool_args = data["toolArgs"]
        if isinstance(tool_args, str):
            try:
                tool_args = _json.loads(tool_args)
            except (ValueError, TypeError):
                pass
        data["tool_input"] = tool_args

    # parameters → tool_input  (Gemini)
    if "parameters" in data and "tool_input" not in data:
        data["tool_input"] = data["parameters"]

    # args → tool_input  (Gemini fallback)
    if "args" in data and "tool_input" not in data:
        data["tool_input"] = data["args"]

    # mcp_context {} → mcp_server / mcp_tool  (Gemini MCP)
    mcp_context = data.get("mcp_context")
    if mcp_context and isinstance(mcp_context, dict):
        server = mcp_context.get("server_name")
        if server and "mcp_server" not in data:
            data["mcp_server"] = server
        tool = mcp_context.get("tool_name")
        if tool and "mcp_tool" not in data:
            data["mcp_tool"] = tool

    # ── Phase 2: MCP prefix/inner extraction + output aliases ──────────
    normalize_mcp_fields(data)

    # ── Phase 3: infer is_error from tool output for shell tools ──────
    _detect_tool_error(data)

    return data


def normalize_mcp_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize MCP-related fields in hook event data.

    Enriches *data* with ``mcp_server``, ``mcp_tool``, and ``tool_output``
    so downstream rule matching doesn't need to handle adapter-specific
    naming conventions.

    Normalizations performed:

    1a. ``mcp__<server>__<tool>`` prefix → ``mcp_server`` / ``mcp_tool``
    1b. For ``call_tool`` / ``mcp__gobby__call_tool``, extract inner
        ``server_name`` / ``tool_name`` from ``tool_input`` (with override
        logic when the ``mcp__`` prefix is present).
    2.  Normalize both ``tool_result`` and ``tool_response`` → ``tool_output``
        (CLI uses ``tool_result``; chat SDK uses ``tool_response``).

    Args:
        data: Event data dict (mutated in place for efficiency, caller
              should pass a copy if the original must be preserved).

    Returns:
        The same *data* dict, enriched with normalized fields.
    """
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}

    # 1a-pre. Normalize single-underscore MCP prefix (Gemini CLI) to canonical
    # double-underscore form.  Gemini sends mcp_<server>_<tool>; canonical is
    # mcp__<server>__<tool>.  Server names never contain underscores, so the
    # first underscore after the "mcp_" prefix delimits the server name.
    if not tool_name.startswith("mcp__") and tool_name.startswith("mcp_"):
        suffix = tool_name[len("mcp_") :]  # e.g. "gobby_call_tool"
        underscore_idx = suffix.find("_")
        if underscore_idx > 0:
            server = suffix[:underscore_idx]
            tool = suffix[underscore_idx + 1 :]
            canonical = f"mcp__{server}__{tool}"
            data["tool_name"] = canonical
            tool_name = canonical

    # 1a. Parse mcp__<server>__<tool> prefix for ALL native MCP calls
    if tool_name.startswith("mcp__") and "mcp_tool" not in data:
        parts = tool_name.split("__", 2)  # ["mcp", "server", "tool"]
        if len(parts) == 3:
            data.setdefault("mcp_server", parts[1])
            data.setdefault("mcp_tool", parts[2])

    # 1b. Extract MCP info from nested tool_input for call_tool calls
    if tool_name in ("call_tool", "mcp__gobby__call_tool", "mcp_gobby_call_tool"):
        inner_server = tool_input.get("server_name")
        inner_tool = tool_input.get("tool_name")
        if tool_name.startswith("mcp__"):
            # Override prefix-parsed values with actual inner target
            if inner_server:
                data["mcp_server"] = inner_server
            if inner_tool:
                data["mcp_tool"] = inner_tool
        else:
            # Plain call_tool — don't overwrite externally-set values
            if inner_server and "mcp_server" not in data:
                data["mcp_server"] = inner_server
            if inner_tool and "mcp_tool" not in data:
                data["mcp_tool"] = inner_tool

        # Coerce string arguments to dict (agents often stringify JSON)
        inner_arguments = tool_input.get("arguments")
        if isinstance(inner_arguments, str):
            try:
                parsed = _json.loads(inner_arguments)
                if isinstance(parsed, dict):
                    tool_input["arguments"] = parsed
                    data["_input_coerced"] = True
            except (ValueError, TypeError):
                pass  # Leave as-is; server-side defense will catch it

    # 2. Normalize tool_result → tool_output (CLI path)
    if "tool_result" in data and "tool_output" not in data:
        data["tool_output"] = data["tool_result"]

    # 2b. Normalize tool_response → tool_output (chat SDK path)
    if "tool_response" in data and "tool_output" not in data:
        data["tool_output"] = data["tool_response"]

    # 2c. Parse string tool_output to dict when possible.
    # Claude Code sends tool_response as JSON text; observers and rules
    # expect a dict.  Parse once here so every consumer gets structured data.
    tool_output = data.get("tool_output")
    if isinstance(tool_output, str):
        try:
            parsed = _json.loads(tool_output)
            if isinstance(parsed, dict):
                data["tool_output"] = parsed
        except (ValueError, TypeError):
            pass  # Non-JSON text output — leave as string

    return data


def _detect_tool_error(data: dict[str, Any]) -> None:
    """Infer ``is_error`` from tool output for shell tools (Phase 3).

    Some adapters set ``is_error`` explicitly via
    ``exit_code`` or ``resultType``.  Claude Code and Gemini do not — they
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
