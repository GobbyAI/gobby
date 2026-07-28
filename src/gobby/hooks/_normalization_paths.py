"""File path and write payload normalization helpers."""

import re as _re
from collections.abc import Mapping
from typing import Any

_APPLY_PATCH_FILE_RE = _re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$")
_APPLY_PATCH_MOVE_RE = _re.compile(r"^\*\*\* Move to: (.+)$")
_PATH_FIELDS = (
    "file_path",
    "filePath",
    "path",
    "target_file",
    "targetFile",
    "old_path",
    "oldPath",
    "source_path",
    "sourcePath",
    "new_path",
    "newPath",
    "target_path",
    "targetPath",
    "destination_path",
    "destinationPath",
)
_PATH_LIST_FIELDS = ("file_paths", "filePaths", "paths")
_PATCH_TEXT_FIELDS = ("command", "patch", "content", "text", "diff")
_NESTED_PAYLOAD_FIELDS = (
    "arguments",
    "args",
    "input",
    "parameters",
    "tool_input",
    "toolInput",
    "tool_output",
    "toolOutput",
    "tool_response",
    "toolResponse",
    "tool_result",
    "toolResult",
    "result",
    "structuredContent",
)


def _append_unique_path(paths: list[str], path: Any) -> None:
    """Append a non-empty path while preserving order."""
    if not isinstance(path, str):
        return
    normalized = path.strip()
    if normalized and normalized not in paths:
        paths.append(normalized)


def _extract_change_paths(change: Any) -> list[str]:
    """Extract every touched path from one file-change entry."""
    if not isinstance(change, Mapping):
        return []
    paths: list[str] = []
    for key in _PATH_FIELDS:
        _append_unique_path(paths, change.get(key))
    return paths


def _extract_change_path(change: Any) -> str | None:
    """Return the first touched path for callers that need one target."""
    paths = _extract_change_paths(change)
    return paths[0] if paths else None


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
        for path in _extract_change_paths(change):
            _append_unique_path(paths, path)

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

    for key in _PATH_LIST_FIELDS:
        file_paths = tool_input.get(key)
        if isinstance(file_paths, list):
            for path in file_paths:
                _append_unique_path(paths, path)

    for key in _PATH_FIELDS:
        _append_unique_path(paths, tool_input.get(key))

    changes = tool_input.get("changes")
    if isinstance(changes, list):
        for change in changes:
            for path in _extract_change_paths(change):
                _append_unique_path(paths, path)

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

    for key in _PATCH_TEXT_FIELDS:
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


def _extract_payload_paths(value: Any, paths: list[str], *, depth: int = 0) -> None:
    """Collect paths from one structured mutation request or response envelope."""
    if depth > 6:
        return
    if isinstance(value, list):
        for item in value:
            _extract_payload_paths(item, paths, depth=depth + 1)
        return
    if not isinstance(value, Mapping):
        return

    for key in _PATH_LIST_FIELDS:
        raw_paths = value.get(key)
        if isinstance(raw_paths, list):
            for path in raw_paths:
                _append_unique_path(paths, path)
    for key in _PATH_FIELDS:
        _append_unique_path(paths, value.get(key))

    for key in _PATCH_TEXT_FIELDS:
        patch_text = value.get(key)
        if not isinstance(patch_text, str):
            continue
        for path in _parse_apply_patch_paths(patch_text):
            _append_unique_path(paths, path)

    changes = value.get("changes")
    if isinstance(changes, list):
        for change in changes:
            _extract_payload_paths(change, paths, depth=depth + 1)

    content_items = value.get("content")
    if isinstance(content_items, list):
        _extract_payload_paths(content_items, paths, depth=depth + 1)

    for key in _NESTED_PAYLOAD_FIELDS:
        nested = value.get(key)
        if isinstance(nested, (Mapping, list)):
            _extract_payload_paths(nested, paths, depth=depth + 1)


def extract_structured_mutation_paths(data: Mapping[str, Any]) -> list[str]:
    """Return ordered, deduplicated file paths from a structured mutation event."""
    paths: list[str] = []
    for field_name in (
        "tool_input",
        "toolInput",
        "tool_output",
        "toolOutput",
        "tool_response",
        "toolResponse",
        "tool_result",
        "toolResult",
        "result",
    ):
        if field_name in data:
            _extract_payload_paths(data[field_name], paths)
    return paths
