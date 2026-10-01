"""Path-scope condition helpers registered in the rule condition namespace."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from gobby.hooks._path_scope import current_project_root, current_tool_cwd, resolve_tool_path
from gobby.workflows.condition_helpers import _event_and_tool_paths, touches_docker_policy_path


def _resolved_write_paths(event_data: Mapping[str, Any] | None, tool_input: Any) -> list[Path]:
    """Resolve every write path, or return [] when any path cannot be resolved.

    Relative paths resolve against the tool cwd (falling back to the project
    root); `..` segments and symlinks resolve before any check.
    """
    data = event_data or {}
    base = current_tool_cwd(data) or current_project_root(data)
    resolved: list[Path] = []
    for raw_path in _event_and_tool_paths(event_data, tool_input):
        path = resolve_tool_path(raw_path, base)
        if path is None:
            return []
        resolved.append(path)
    return resolved


def write_paths_within(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
    dirs: Iterable[str],
) -> bool:
    """True when every write path equals or descends from one allowed directory.

    Containment is component-wise, so `docs-other/` is outside `docs/`. A
    relative directory is project-relative. An empty path set is False.
    """
    paths = _resolved_write_paths(event_data, tool_input)
    if not paths:
        return False
    root = current_project_root(event_data or {})
    allowed = [
        directory
        for directory in (
            resolve_tool_path(raw, root) for raw in ([dirs] if isinstance(dirs, str) else dirs)
        )
        if directory is not None
    ]
    return all(any(path.is_relative_to(directory) for directory in allowed) for path in paths)


def write_paths_match(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
    pattern: str,
) -> bool:
    """True when every resolved absolute write path fullmatches `pattern`.

    A leading `~/` anchors the pattern at the current user's resolved home, so
    shipped rules never name one machine's home directory.
    """
    if pattern.startswith("~/"):
        pattern = re.escape(str(Path.home().resolve())) + pattern[1:]
    paths = _resolved_write_paths(event_data, tool_input)
    return bool(paths) and all(re.fullmatch(pattern, str(path)) for path in paths)


PATH_CONDITION_HELPERS: dict[str, Callable[..., Any]] = {
    "touches_docker_policy_path": touches_docker_policy_path,
    "write_paths_within": write_paths_within,
    "write_paths_match": write_paths_match,
}
