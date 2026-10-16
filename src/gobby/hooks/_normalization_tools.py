"""Top-level tool-field normalization orchestration."""

from typing import Any

from gobby.hooks._normalization_canonical import _normalize_shell_tool_metadata
from gobby.hooks._normalization_mcp import normalize_mcp_fields
from gobby.hooks._normalization_metadata import _build_canonical_tool_metadata
from gobby.hooks._normalization_paths import (
    _compact_tool_name,
    _extract_tool_input_paths,
    _is_structured_file_mutation,
    _normalize_apply_patch_input,
    _normalize_file_change_input,
    _setdefault_tool_input_paths,
    _structured_write_paths,
    extract_structured_mutation_paths,
)
from gobby.hooks._normalization_shell import _get_command_text, canonicalize_shell_tool_name
from gobby.hooks._normalization_tool_input import (
    TOOL_INPUT_ERROR_FIELD,
    decode_string_tool_input,
    is_non_object_tool_input,
    mark_tool_input_unavailable,
    tool_input_error,
    tool_input_source,
)
from gobby.hooks._path_scope import apply_path_scope_metadata, current_tool_cwd
from gobby.hooks.code_navigation import (
    line_count_from_tool_input,
    search_navigation_metadata,
    source_read_navigation_metadata,
)
from gobby.hooks.code_navigation_recovery import annotate_navigation, annotate_navigation_outcome
from gobby.hooks.tool_outcomes import normalize_tool_outcome

_TOOL_INPUT_FIELD_ALIASES = (
    ("cmd", "command"),
    ("CommandLine", "command"),
    ("Cwd", "cwd"),
    ("TargetFile", "file_path"),
    ("AbsolutePath", "file_path"),
    ("DirectoryPath", "file_path"),
)

_CANONICAL_READ_TOOL_NAMES = frozenset({"read"})


def _set_canonical_tool_metadata(data: dict[str, Any], *, shell_metadata: bool = True) -> None:
    """Annotate events with canonical read/search/write semantics across CLIs.

    ``shell_metadata=False`` skips Bash command analysis, which can run Git.
    """
    tool_name = data.get("tool_name")
    tool_input = data.get("tool_input")

    metadata: dict[str, Any] = {}
    tool_name_lower = tool_name.lower() if isinstance(tool_name, str) else ""

    is_structured_mutation = _is_structured_file_mutation(data, tool_name)
    if tool_name_lower in _CANONICAL_READ_TOOL_NAMES:
        metadata = _build_canonical_tool_metadata("read")
    elif is_structured_mutation:
        canonical_paths = extract_structured_mutation_paths(data)
        write_paths = _structured_write_paths(data, tool_name, canonical_paths)
        metadata = _build_canonical_tool_metadata(
            "write",
            paths=canonical_paths,
            write_paths=write_paths or None,
            repo_mutation=True,
        )
        metadata["canonical_structured_mutation"] = True
        metadata["canonical_file_paths"] = canonical_paths
    elif tool_name_lower in {"grep_search", "grep"}:
        metadata = _build_canonical_tool_metadata("search")
    elif tool_name == "Bash" and shell_metadata:
        command = _get_command_text(tool_input)
        if command:
            metadata = _normalize_shell_tool_metadata(command, base_cwd=current_tool_cwd(data))
    elif "mcp_server" in data and "mcp_tool" in data:
        metadata = _build_canonical_tool_metadata("mcp")

    canonical_file_paths = metadata.get("canonical_file_paths")
    if not isinstance(canonical_file_paths, list):
        canonical_file_paths = []

    if not canonical_file_paths:
        canonical_file_paths = _extract_tool_input_paths(tool_input)
        if canonical_file_paths:
            metadata["canonical_file_paths"] = canonical_file_paths

    if canonical_file_paths and "canonical_file_path" not in metadata:
        metadata["canonical_file_path"] = canonical_file_paths[0]

    if (
        metadata.get("canonical_tool_kind") == "read"
        and "canonical_code_navigation_broad" not in metadata
        and not metadata.get("canonical_code_navigation_segments")
        and not metadata.get("_canonical_pending_source_reads")
    ):
        line_count = line_count_from_tool_input(tool_input)
        read_scope = "line_range" if line_count is not None else "full_file"
        metadata.update(
            source_read_navigation_metadata(
                canonical_file_paths,
                line_count=line_count,
                read_scope=read_scope,
            )
        )

    if (
        metadata.get("canonical_tool_kind") == "search"
        and not metadata.get("canonical_code_index_navigation")
        and "canonical_code_navigation_broad" not in metadata
    ):
        metadata.update(search_navigation_metadata(canonical_file_paths))

    # Unproved variable operands remain source reads after all enclosing proofs fail.
    for record in metadata.pop("_canonical_pending_source_reads", ()):
        metadata.setdefault("canonical_code_navigation_segments", []).append(
            {**record["scope"], "canonical_file_paths": list(record["paths"])}
        )
    if segments := metadata.get("canonical_code_navigation_segments"):
        metadata["canonical_code_navigation_broad"] = any(
            segment.get("canonical_code_navigation_broad") for segment in segments
        )
    apply_path_scope_metadata(data, metadata, canonical_file_paths)
    annotate_navigation(data, metadata)

    if canonical_file_paths:
        _setdefault_tool_input_paths(tool_input, canonical_file_paths)

    data.update(metadata)


def provider_native_input_updates(
    raw_tool_input: dict[str, Any], updates: dict[str, Any]
) -> dict[str, Any]:
    """Move canonical input updates onto the field names the CLI actually sent.

    Normalization copies provider fields such as AGY's ``CommandLine`` onto
    canonical keys, so rewrites are written against ``command``. A replacement
    payload built from the raw input would otherwise keep the old provider value
    and add a key the CLI never declared (#23771). An update that already names
    the provider field wins over its canonical twin.
    """
    native = dict(updates)
    for provider_name, canonical_name in _TOOL_INPUT_FIELD_ALIASES:
        if (
            canonical_name in native
            and provider_name in raw_tool_input
            and canonical_name not in raw_tool_input
        ):
            native.setdefault(provider_name, native.pop(canonical_name))
    return native


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
    return _normalize_tool_fields(data, shell_metadata=True)


def normalize_tool_display_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize like :func:`normalize_tool_fields` without Bash command metadata.

    Shell command analysis can run Git, so event-loop callers that read only
    names, inputs and outcomes use this; the hook engine derives the canonical
    shell metadata off the loop.
    """
    return _normalize_tool_fields(data, shell_metadata=False)


def _normalize_tool_fields(data: dict[str, Any], *, shell_metadata: bool) -> dict[str, Any]:
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
    # A list, number, or bool, sent directly or as JSON text, is marked before any
    # Write or apply_patch coercion could turn it into an object (#23179).
    if decoded_string and "tool_input" in data and not isinstance(data["tool_input"], dict):
        mark_tool_input_unavailable(data, source, "non_object_json")
    elif "tool_name" in data and is_non_object_tool_input(data.get("tool_input")):
        mark_tool_input_unavailable(data, source, "non_object")
    marked = tool_input_error(data) is not None

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
        if not marked:
            tool_input = _normalize_apply_patch_input(tool_input)
            data["tool_input"] = tool_input
    elif data.get("tool_name") == "Write":
        normalized_input = _normalize_file_change_input(tool_input)
        if normalized_input is not tool_input:
            data["tool_input"] = normalized_input
            tool_input = normalized_input

    if prior_error is not None and "tool_input" not in data:
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
    _set_canonical_tool_metadata(data, shell_metadata=shell_metadata)

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
