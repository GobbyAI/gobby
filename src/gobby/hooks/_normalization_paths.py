"""File path and write payload normalization helpers."""

import re as _re
from typing import Any

_APPLY_PATCH_FILE_RE = _re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$")
_APPLY_PATCH_MOVE_RE = _re.compile(r"^\*\*\* Move to: (.+)$")


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
