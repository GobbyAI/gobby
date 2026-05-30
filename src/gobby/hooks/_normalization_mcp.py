"""MCP tool-field normalization helpers."""

import json as _json
from typing import Any


def _parse_json_object(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    try:
        parsed = _json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_mcp_content_object(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        parsed = _parse_json_object(item.get("text"))
        if parsed is not None:
            return parsed
    return None


def _unwrap_mcp_tool_output(
    tool_output: Any,
    *,
    _depth: int = 0,
    _max_depth: int = 8,
) -> Any:
    if _depth >= _max_depth:
        return tool_output
    if not isinstance(tool_output, dict):
        return tool_output

    structured_content = tool_output.get("structuredContent")
    if structured_content is not None:
        return structured_content

    result = tool_output.get("result")
    if isinstance(result, dict):
        nested_structured = result.get("structuredContent")
        if nested_structured is not None:
            return nested_structured

    parsed_content = _extract_mcp_content_object(tool_output.get("content"))
    if parsed_content is not None:
        return parsed_content

    if isinstance(result, dict):
        nested_content = _extract_mcp_content_object(result.get("content"))
        if nested_content is not None:
            return nested_content

    parsed_output = _parse_json_object(tool_output.get("output"))
    if parsed_output is not None:
        return _unwrap_mcp_tool_output(
            parsed_output,
            _depth=_depth + 1,
            _max_depth=_max_depth,
        )

    return tool_output


def normalize_mcp_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize MCP-related fields in hook event data.

    Enriches *data* with ``mcp_server``, ``mcp_tool``, and ``tool_output``
    so downstream rule matching doesn't need to handle adapter-specific
    naming conventions.

    Normalizations performed:

    1a. ``mcp__<server>__<tool>`` prefix -> ``mcp_server`` / ``mcp_tool``
    1b. For ``call_tool`` / ``mcp__gobby__call_tool``, extract inner
        ``server_name`` / ``tool_name`` from ``tool_input`` (with override
        logic when the ``mcp__`` prefix is present).
    2.  Normalize both ``tool_result`` and ``tool_response`` -> ``tool_output``
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

    # 1a-pre. Normalize triple-underscore MCP prefix (Droid CLI) to canonical
    # double-underscore form. Droid sends <server>___<tool>; canonical is
    # mcp__<server>__<tool>. The triple separator is unambiguous even when
    # server names contain underscores.
    if not tool_name.startswith("mcp__") and "___" in tool_name:
        server, _, tool = tool_name.partition("___")
        if server and tool:
            canonical = f"mcp__{server}__{tool}"
            data["tool_name"] = canonical
            tool_name = canonical

    # 1a. Parse mcp__<server>__<tool> prefix for ALL native MCP calls
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)  # ["mcp", "server", "tool"]
        if len(parts) == 3:
            data.setdefault("mcp_server", parts[1])
            data.setdefault("mcp_tool", parts[2])

    # 1b. Extract MCP info from nested tool_input for call_tool calls
    if tool_name in ("call_tool", "mcp__gobby__call_tool", "mcp_gobby_call_tool"):
        inner_server = tool_input.get("server_name")
        inner_tool = tool_input.get("tool_name")
        if tool_name.startswith("mcp__") and (inner_server or inner_tool):
            # The gobby call_tool wrapper is not the semantic target. Clear
            # prefix-parsed wrapper fields, then set the inner target when present.
            data.pop("mcp_server", None)
            data.pop("mcp_tool", None)
            if inner_server:
                data["mcp_server"] = inner_server
            if inner_tool:
                data["mcp_tool"] = inner_tool
        else:
            # Plain call_tool - don't overwrite externally-set values
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

    # 2. Normalize tool_result -> tool_output (CLI path)
    if "tool_result" in data and "tool_output" not in data:
        data["tool_output"] = data["tool_result"]

    # 2b. Normalize tool_response -> tool_output (chat SDK path)
    if "tool_response" in data and "tool_output" not in data:
        data["tool_output"] = data["tool_response"]

    # 2c. Parse string tool_output to dict when possible.
    # Claude Code sends tool_response as JSON text; observers and rules
    # expect a dict. Parse once here so every consumer gets structured data.
    tool_output = data.get("tool_output")
    if isinstance(tool_output, str):
        parsed = _parse_json_object(tool_output)
        if parsed is not None:
            data["tool_output"] = parsed

    # 2d. Unwrap standard MCP result envelopes. Native MCP hooks preserve the
    # outer {content, structuredContent, isError} wrapper, but rules and step
    # enforcement need the semantic tool payload itself.
    if "tool_output" in data:
        data["tool_output"] = _unwrap_mcp_tool_output(data["tool_output"])

    return data
