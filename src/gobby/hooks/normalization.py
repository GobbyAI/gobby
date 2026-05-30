"""Shared field normalization for hook events.

Provides normalization so every CLI adapter produces consistent
canonical fields (``tool_name``, ``tool_input``, ``tool_output``,
``mcp_server``, ``mcp_tool``, ``is_error``) and broadcast payloads satisfy
strict hook schemas without dropping provider-specific fields.

Tool normalization is split into two phases:

- ``normalize_tool_fields``: flatten CLI-specific field aliases
- ``normalize_mcp_fields``: MCP prefix/inner extraction + output aliases
"""

import json as _json
import re as _re
import shlex as _shlex
from collections.abc import Mapping
from typing import Any

from gobby.hooks.code_navigation import (
    count_option_line_count,
    gcode_navigation_metadata,
    line_count_from_tool_input,
    search_navigation_metadata,
    sed_line_count,
    shell_command_name,
    source_read_navigation_metadata,
)

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

_NOTIFICATION_TYPE_FIELDS = ("notification_type", "notificationType", "type", "level", "severity")
_NOTIFICATION_MESSAGE_FIELDS = ("message", "title", "reason")
_NOTIFICATION_SEVERITY_VALUES = frozenset({"info", "warning", "error"})
_DEFAULT_NOTIFICATION_TYPE = "general"
_DEFAULT_NOTIFICATION_MESSAGE = "Notification event received"

# Pattern to detect non-zero exit codes in tool output text.
# Matches: "Exit code: 1", "exit code 127", "Error: Exit code 2", etc.
_EXIT_CODE_RE = _re.compile(r"[Ee]xit.?code[:\s]+(\d+)")
_APPLY_PATCH_FILE_RE = _re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$")
_APPLY_PATCH_MOVE_RE = _re.compile(r"^\*\*\* Move to: (.+)$")
_SHELL_CHAIN_TOKENS = frozenset({"&&", "||", ";", "|"})
_SHELL_INPUT_REDIRECTION_TOKENS = frozenset({"<", "<<", "<<<"})
_SHELL_OUTPUT_REDIRECTION_TOKENS = frozenset({">", ">>", "1>", "1>>"})
_SHELL_CONTROL_TOKENS = (
    _SHELL_CHAIN_TOKENS | _SHELL_INPUT_REDIRECTION_TOKENS | _SHELL_OUTPUT_REDIRECTION_TOKENS
)
_CANONICAL_READ_TOOL_NAMES = frozenset({"read"})
_CANONICAL_WRITE_TOOL_NAMES = frozenset(
    {
        "edit",
        "edit_file",
        "notebook_edit",
        "notebookedit",
        "replace",
        "write",
        "write_file",
    }
)

# Characters that strongly imply an inline sed/awk script rather than a file path.
_SCRIPT_LIKE_CHARS = frozenset({"{", "}", "$", ";", "(", ")"})


def is_shell_tool(tool_name: Any) -> bool:
    """Return True when ``tool_name`` represents shell command execution."""
    return isinstance(tool_name, str) and tool_name in _SHELL_TOOLS


def canonicalize_shell_tool_name(tool_name: Any) -> Any:
    """Normalize shell aliases to the canonical ``Bash`` tool name."""
    if is_shell_tool(tool_name):
        return "Bash"
    return tool_name


def _non_empty_string(value: Any) -> str | None:
    """Return stripped string values that still contain text."""
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if normalized:
        return normalized
    return None


def notification_type_from_payload(data: Mapping[str, Any]) -> str:
    """Select the canonical notification type from provider-specific aliases."""
    for field_name in _NOTIFICATION_TYPE_FIELDS:
        value = _non_empty_string(data.get(field_name))
        if value:
            return value
    return _DEFAULT_NOTIFICATION_TYPE


def notification_message_from_payload(data: Mapping[str, Any]) -> str:
    """Select the canonical notification message from provider-specific aliases."""
    for field_name in _NOTIFICATION_MESSAGE_FIELDS:
        value = _non_empty_string(data.get(field_name))
        if value:
            return value
    return _DEFAULT_NOTIFICATION_MESSAGE


def _notification_severity_from_payload(data: Mapping[str, Any]) -> str | None:
    """Return a valid notification severity from level/severity aliases."""
    for field_name in ("severity", "level"):
        value = _non_empty_string(data.get(field_name))
        if not value:
            continue

        normalized = value.lower()
        if normalized in _NOTIFICATION_SEVERITY_VALUES:
            return normalized
    return None


def normalize_notification_input(data: dict[str, Any]) -> dict[str, Any]:
    """Backfill strict NotificationInput fields in place and return the same dict."""
    data["notification_type"] = notification_type_from_payload(data)
    data["message"] = notification_message_from_payload(data)

    severity = _notification_severity_from_payload(data)
    if severity:
        data["severity"] = severity

    return data


def _append_unique_path(paths: list[str], path: Any) -> None:
    """Append a non-empty path while preserving order."""
    if not isinstance(path, str):
        return
    normalized = path.strip()
    if normalized and normalized not in paths:
        paths.append(normalized)


def _extract_change_path(change: Any) -> str | None:
    """Extract a touched file path from a file-change dict."""
    if not isinstance(change, dict):
        return None

    for key in (
        "file_path",
        "path",
        "new_path",
        "newPath",
        "target_path",
        "targetPath",
    ):
        value = change.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _normalize_file_change_input(tool_input: Any) -> Any:
    """Normalize app-server style file-change lists into canonical Write input."""
    if isinstance(tool_input, list):
        normalized_input: dict[str, Any] = {"changes": tool_input}
        changes = tool_input
    elif isinstance(tool_input, dict) and isinstance(tool_input.get("changes"), list):
        normalized_input = dict(tool_input)
        changes = normalized_input["changes"]
    else:
        return tool_input

    paths: list[str] = []
    for change in changes:
        _append_unique_path(paths, _extract_change_path(change))

    if paths:
        normalized_input.setdefault("file_path", paths[0])
        if len(paths) > 1:
            normalized_input.setdefault("file_paths", paths)

    return normalized_input


def _extract_tool_input_paths(tool_input: Any) -> list[str]:
    """Extract canonical file paths from a normalized tool input payload."""
    if not isinstance(tool_input, dict):
        return []

    paths: list[str] = []

    file_paths = tool_input.get("file_paths")
    if isinstance(file_paths, list):
        for path in file_paths:
            _append_unique_path(paths, path)

    _append_unique_path(paths, tool_input.get("file_path"))

    changes = tool_input.get("changes")
    if isinstance(changes, list):
        for change in changes:
            _append_unique_path(paths, _extract_change_path(change))

    return paths


def _setdefault_tool_input_paths(tool_input: Any, paths: list[str]) -> None:
    """Backfill normalized file_path/file_paths into tool_input when derivable."""
    if not isinstance(tool_input, dict) or not paths:
        return

    tool_input.setdefault("file_path", paths[0])
    if len(paths) > 1:
        tool_input.setdefault("file_paths", paths)


def _extract_apply_patch_text(tool_input: Any) -> str | None:
    """Extract raw patch text from apply_patch inputs."""
    if isinstance(tool_input, str):
        return tool_input

    if not isinstance(tool_input, dict):
        return None

    for key in ("patch", "content", "text", "diff"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value

    return None


def _parse_apply_patch_paths(patch_text: str) -> list[str]:
    """Extract touched paths from apply_patch freeform patch text."""
    paths: list[str] = []

    for raw_line in patch_text.splitlines():
        line = raw_line.strip()
        file_match = _APPLY_PATCH_FILE_RE.match(line)
        if file_match:
            _append_unique_path(paths, file_match.group(1))
            continue

        move_match = _APPLY_PATCH_MOVE_RE.match(line)
        if move_match:
            _append_unique_path(paths, move_match.group(1))

    return paths


def _normalize_apply_patch_input(tool_input: Any) -> dict[str, Any]:
    """Normalize apply_patch payloads into canonical Write input."""
    normalized_input: dict[str, Any] = dict(tool_input) if isinstance(tool_input, dict) else {}
    patch_text = _extract_apply_patch_text(tool_input)

    if patch_text is not None:
        normalized_input.setdefault("patch", patch_text)

    if isinstance(normalized_input.get("path"), str) and "file_path" not in normalized_input:
        normalized_input["file_path"] = normalized_input["path"]

    if patch_text:
        paths = _parse_apply_patch_paths(patch_text)
        if paths:
            normalized_input.setdefault("file_path", paths[0])
            if len(paths) > 1:
                normalized_input.setdefault("file_paths", paths)

    return normalized_input


def _get_command_text(tool_input: Any) -> str | None:
    """Extract a shell command string from normalized tool input."""
    if not isinstance(tool_input, dict):
        return None

    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return command

    cmd = tool_input.get("cmd")
    if isinstance(cmd, str) and cmd.strip():
        return cmd

    return None


def _shell_positional_args(parts: list[str]) -> list[str]:
    """Return non-option shell args, excluding obvious control operators."""
    return [
        part
        for part in parts[1:]
        if part and part not in _SHELL_CONTROL_TOKENS and not part.startswith("-")
    ]


def _looks_file_like(candidate: str) -> bool:
    """Return True when ``candidate`` looks like a file path, not an inline script.

    Used to gate sed/awk's last positional arg so we don't classify an inline
    script (``'s/foo/bar/'``, ``'{print $1}'``) as a file that was read.
    """
    if not candidate or any(ch in candidate for ch in _SCRIPT_LIKE_CHARS):
        return False
    # Must carry a path separator or an extension-like dot that isn't a leading/solo dot.
    if "/" in candidate:
        return True
    if "." in candidate and candidate not in {".", ".."}:
        return True
    return False


def _looks_path_target(candidate: str) -> bool:
    """Return True when ``candidate`` is a plausible shell path target."""
    if not candidate or candidate in _SHELL_CONTROL_TOKENS or candidate == "-":
        return False
    if candidate.startswith("-") or candidate.startswith("&"):
        return False
    if any(ch in candidate for ch in _SCRIPT_LIKE_CHARS):
        return False
    return True


def _build_canonical_tool_metadata(
    kind: str,
    *,
    paths: list[str] | None = None,
    repo_mutation: bool = False,
    confidence: str = "high",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a canonical metadata payload for a tool event."""
    data: dict[str, Any] = {
        "canonical_tool_kind": kind,
        "canonical_tool_confidence": confidence,
    }
    if paths:
        data["canonical_file_paths"] = paths
        data["canonical_file_path"] = paths[0]
    if repo_mutation:
        data["canonical_repo_mutation"] = True
    if extra:
        data.update(extra)
    return data


def _has_sed_inplace_option(parts: list[str]) -> bool:
    """Return True when a sed command performs in-place editing."""
    for part in parts[1:]:
        if part in {"-i", "--in-place"}:
            return True
        if part.startswith("-i"):
            return True
        if part.startswith("--in-place="):
            return True
    return False


def _has_perl_inplace_option(parts: list[str]) -> bool:
    """Return True when a perl command edits files in place."""
    for part in parts[1:]:
        if part == "-pi" or part.startswith("-pi"):
            return True
        if part == "-i" or part.startswith("-i"):
            return True
    return False


def _extract_redirection_paths(parts: list[str]) -> list[str]:
    """Extract explicit output redirection targets from shell tokens."""
    paths: list[str] = []
    for idx, token in enumerate(parts[:-1]):
        if token not in _SHELL_OUTPUT_REDIRECTION_TOKENS:
            continue
        candidate = parts[idx + 1]
        if _looks_path_target(candidate):
            _append_unique_path(paths, candidate)
    return paths


def _normalize_shell_tool_metadata(command: str) -> dict[str, Any]:
    """Infer canonical semantics from simple shell read/search/write commands."""
    try:
        parts = _shlex.split(command)
    except ValueError:
        return {}

    if not parts or any(token in _SHELL_CHAIN_TOKENS for token in parts):
        return {}

    if any(token in _SHELL_INPUT_REDIRECTION_TOKENS for token in parts):
        return {}

    cmd = shell_command_name(parts[0])
    gcode_metadata = gcode_navigation_metadata(parts)
    if gcode_metadata:
        kind, extra = gcode_metadata
        return _build_canonical_tool_metadata(kind, extra=extra)

    redirection_paths = _extract_redirection_paths(parts)
    if redirection_paths:
        return _build_canonical_tool_metadata(
            "write",
            paths=redirection_paths,
            repo_mutation=True,
        )

    if cmd in {"rg", "grep", "git"}:
        if cmd == "git" and len(parts) > 1 and parts[1] != "grep":
            return {}
        return _build_canonical_tool_metadata("search", extra=search_navigation_metadata())

    if cmd == "find":
        return _build_canonical_tool_metadata("search", extra=search_navigation_metadata())

    if cmd in {"cat", "head", "tail", "bat", "nl"}:
        positional = _shell_positional_args(parts)
        paths = [candidate for candidate in positional if _looks_file_like(candidate)]
        line_count = count_option_line_count(parts) if cmd in {"head", "tail"} else None
        read_scope = "line_range" if line_count is not None else "full_file"
        return _build_canonical_tool_metadata(
            "read",
            paths=paths or None,
            extra=source_read_navigation_metadata(
                paths,
                line_count=line_count,
                read_scope=read_scope,
            ),
        )

    if cmd == "sed" and len(parts) >= 2:
        positional = _shell_positional_args(parts)
        candidate = positional[-1] if positional else None
        if _has_sed_inplace_option(parts):
            paths = [candidate] if candidate and _looks_path_target(candidate) else []
            return _build_canonical_tool_metadata(
                "write",
                paths=paths or None,
                repo_mutation=True,
            )
        paths = [item for item in positional if _looks_file_like(item)]
        line_count = sed_line_count(parts, positional)
        read_scope = "line_range" if line_count is not None else "full_file"
        return _build_canonical_tool_metadata(
            "read",
            paths=paths or None,
            extra=source_read_navigation_metadata(
                paths,
                line_count=line_count,
                read_scope=read_scope,
            ),
        )

    if cmd == "perl" and _has_perl_inplace_option(parts):
        positional = _shell_positional_args(parts)
        candidate = positional[-1] if positional else None
        paths = [candidate] if candidate and _looks_path_target(candidate) else []
        return _build_canonical_tool_metadata(
            "write",
            paths=paths or None,
            repo_mutation=True,
        )

    if cmd == "tee":
        positional = _shell_positional_args(parts)
        paths = [candidate for candidate in positional if _looks_path_target(candidate)]
        return _build_canonical_tool_metadata(
            "write",
            paths=paths or None,
            repo_mutation=True,
        )

    if cmd in {"touch", "rm", "mkdir", "rmdir"}:
        positional = _shell_positional_args(parts)
        paths = [candidate for candidate in positional if _looks_path_target(candidate)]
        return _build_canonical_tool_metadata(
            "write",
            paths=paths or None,
            repo_mutation=True,
        )

    if cmd in {"cp", "mv", "install"}:
        positional = _shell_positional_args(parts)
        candidate = positional[-1] if positional else None
        paths = [candidate] if candidate and _looks_path_target(candidate) else []
        return _build_canonical_tool_metadata(
            "write",
            paths=paths or None,
            repo_mutation=True,
        )

    if cmd == "truncate":
        truncate_positional: list[str] = []
        skip_next = False
        for part in parts[1:]:
            if skip_next:
                skip_next = False
                continue
            if part in _SHELL_CONTROL_TOKENS or not part:
                continue
            if part in {"-s", "--size"}:
                skip_next = True
                continue
            if part.startswith("--size=") or part.startswith("-"):
                continue
            truncate_positional.append(part)
        paths = [candidate for candidate in truncate_positional if _looks_path_target(candidate)]
        return _build_canonical_tool_metadata(
            "write",
            paths=paths or None,
            repo_mutation=True,
        )

    return {}


def _set_canonical_tool_metadata(data: dict[str, Any]) -> None:
    """Annotate events with canonical read/search/write semantics across CLIs."""
    tool_name = data.get("tool_name")
    tool_input = data.get("tool_input")

    metadata: dict[str, Any] = {}
    tool_name_lower = tool_name.lower() if isinstance(tool_name, str) else ""

    if tool_name_lower in _CANONICAL_READ_TOOL_NAMES:
        metadata = _build_canonical_tool_metadata("read")
    elif tool_name_lower in _CANONICAL_WRITE_TOOL_NAMES:
        metadata = _build_canonical_tool_metadata("write", repo_mutation=True)
    elif tool_name_lower in {"grep_search", "grep"}:
        metadata = _build_canonical_tool_metadata("search")
    elif tool_name == "Bash":
        command = _get_command_text(tool_input)
        if command:
            metadata = _normalize_shell_tool_metadata(command)
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
        metadata.update(search_navigation_metadata())

    if metadata.get("canonical_tool_kind") == "write" and "canonical_repo_mutation" not in metadata:
        metadata["canonical_repo_mutation"] = True

    if canonical_file_paths:
        _setdefault_tool_input_paths(tool_input, canonical_file_paths)

    data.update(metadata)


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

    # Normalize tool_input internal fields (e.g., path → file_path for Gemini)
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

    # ── Phase 2.5: infer canonical read/search/write semantics ────────
    _set_canonical_tool_metadata(data)

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
