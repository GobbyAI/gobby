"""Normalize provider tool arguments for transcript-derived task evidence."""

from __future__ import annotations

import os
from typing import Any

_COMMAND_KEYS = ("cmd", "command", "script")
_PATH_KEYS = ("file_path", "target_file", "path", "notebook_path", "TargetFile")


def extract_command(arguments: dict[str, Any]) -> str:
    """Return the first non-empty shell command exposed by a provider tool."""
    for key in _COMMAND_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_tool_name(tool_name: str) -> str:
    """Reduce provider-qualified tool names to their normalized basename."""
    normalized = tool_name.casefold().replace("-", "_")
    for separator in ("__", ".", "/"):
        if separator in normalized:
            normalized = normalized.rsplit(separator, 1)[-1]
    return normalized


def extract_edit_paths(
    tool_name: str,
    arguments: dict[str, Any],
    repo_path: str,
    *,
    require_proven_checkout: bool = False,
) -> set[str]:
    """Extract edits, retaining call-time checkout provenance when required."""
    values: set[str] = set()
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            resolved = resolve_edit_path(value, arguments, repo_path, require_proven_checkout)
            if resolved is not None:
                values.add(resolved)
    if tool_name in {"apply_patch", "exec"}:
        raw = arguments.get("raw") or arguments.get("patch") or arguments.get("input")
        if isinstance(raw, str):
            if tool_name == "exec":
                if "tools.apply_patch" not in raw:
                    return values
                raw = raw.replace(r"\r", "\r").replace(r"\n", "\n")
            for line in raw.splitlines():
                for prefix in ("*** Add File: ", "*** Delete File: ", "*** Update File: "):
                    if line.startswith(prefix):
                        resolved = resolve_edit_path(
                            line.removeprefix(prefix), arguments, repo_path, require_proven_checkout
                        )
                        if resolved is not None:
                            values.add(resolved)
    return values


def resolve_edit_path(
    path: str,
    arguments: dict[str, Any],
    repo_path: str,
    require_proven_checkout: bool,
) -> str | None:
    """Resolve a relative edit only when the tool call names an absolute cwd."""
    if not require_proven_checkout:
        return normalize_known_path(path, repo_path)
    if os.path.isabs(path):
        return os.path.realpath(path)
    for key in ("workdir", "cwd"):
        cwd = arguments.get(key)
        if isinstance(cwd, str) and os.path.isabs(cwd):
            return os.path.realpath(os.path.join(cwd, path))
    return None


def match_task_file(
    path: str,
    task_files: set[str],
    repo_path: str | None = None,
    task_checkout_paths: frozenset[tuple[str, str]] | None = None,
) -> str | None:
    """Map an edit only from an exact task-attributed checkout/path pair."""
    if task_checkout_paths is not None:
        if not os.path.isabs(path):
            return None
        absolute = os.path.realpath(path)
        for root, task_path in task_checkout_paths:
            try:
                relative = os.path.relpath(absolute, root).replace(os.sep, "/")
            except ValueError:
                continue
            if relative == task_path and relative in task_files:
                return relative
        return None
    if path in task_files:
        return path
    if not (os.path.isabs(path) or path.startswith("../")):
        return None
    return max(
        (task_file for task_file in task_files if path.endswith(f"/{task_file}")),
        key=len,
        default=None,
    )


def normalize_known_path(path: str, repo_path: str) -> str:
    """Normalize a provider path relative to the repository when possible."""
    normalized = os.path.normpath(path)
    if os.path.isabs(normalized):
        try:
            normalized = os.path.relpath(normalized, repo_path)
        except ValueError:
            pass
    while normalized.startswith(f".{os.sep}"):
        normalized = normalized[2:]
    return normalized.replace(os.sep, "/")


__all__ = [
    "extract_command",
    "extract_edit_paths",
    "match_task_file",
    "normalize_known_path",
    "normalize_tool_name",
]
