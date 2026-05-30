"""Canonical tool metadata inference."""

import shlex as _shlex
from collections.abc import Mapping
from typing import Any

from gobby.hooks._normalization_paths import (
    _extract_tool_input_paths,
    _setdefault_tool_input_paths,
)
from gobby.hooks._normalization_shell import (
    _SHELL_CHAIN_TOKENS,
    _SHELL_CONTROL_TOKENS,
    _SHELL_INPUT_REDIRECTION_TOKENS,
    _extract_redirection_paths,
    _get_command_text,
    _has_perl_inplace_option,
    _has_sed_inplace_option,
    _looks_file_like,
    _looks_path_target,
    _shell_positional_args,
)
from gobby.hooks.code_navigation import (
    count_option_line_count,
    gcode_navigation_metadata,
    line_count_from_tool_input,
    search_navigation_metadata,
    sed_line_count,
    shell_command_name,
    source_read_navigation_metadata,
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
