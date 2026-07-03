"""Path scope helpers for hook normalization."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gobby.paths import get_gobby_home
from gobby.utils.project_context import find_project_root

_SCRATCHPAD_MARKERS = frozenset(
    {
        "agent-scratchpad",
        "agent-scratchpads",
        "gobby-agent-scratchpad",
        "gobby-agent-scratchpads",
        "scratchpad",
        "scratchpads",
    }
)
_SCRATCHPAD_PREFIXES = (
    "agent-scratchpad",
    "codex-scratchpad",
    "gobby-agent-scratchpad",
    "gobby-scratchpad",
)


def apply_path_scope_metadata(
    event_data: Mapping[str, Any],
    metadata: dict[str, Any],
    paths: Sequence[str],
) -> None:
    """Annotate canonical metadata with current-project path scope."""
    project_root = current_project_root(event_data)
    cwd = current_tool_cwd(event_data)

    if metadata.get("canonical_tool_kind") == "write":
        metadata["canonical_repo_mutation"] = paths_may_touch_project(
            paths,
            cwd=cwd,
            project_root=project_root,
        )

    if metadata.get("canonical_code_navigation_action"):
        metadata["canonical_code_navigation_repo_scope"] = code_navigation_may_touch_project(
            paths,
            cwd=cwd,
            project_root=project_root,
        )


def current_tool_cwd(event_data: Mapping[str, Any]) -> Path | None:
    """Resolve the directory relative tool paths should be interpreted from."""
    tool_input = event_data.get("tool_input")
    if isinstance(tool_input, Mapping):
        for key in ("workdir", "cwd"):
            cwd = _resolve_base_dir(tool_input.get(key))
            if cwd is not None:
                return cwd
    return _resolve_base_dir(event_data.get("cwd"))


def current_project_root(event_data: Mapping[str, Any]) -> Path | None:
    """Resolve the current project root from hook metadata or cwd."""
    root = _resolve_base_dir(event_data.get("project_path"))
    if root is not None:
        return root

    cwd = current_tool_cwd(event_data)
    if cwd is None:
        return None
    try:
        return find_project_root(cwd)
    except (OSError, RuntimeError):
        return None


def paths_may_touch_project(
    paths: Sequence[str],
    *,
    cwd: Path | None,
    project_root: Path | None,
) -> bool:
    """Return True when write paths may mutate the current project."""
    if not paths:
        return True

    for raw_path in paths:
        path = resolve_tool_path(raw_path, cwd)
        if path is None:
            return True
        if project_root is not None and _is_relative_to(path, project_root):
            return True
        if project_root is None and not _is_known_external_path(path):
            return True
    return False


def code_navigation_may_touch_project(
    paths: Sequence[str],
    *,
    cwd: Path | None,
    project_root: Path | None,
) -> bool:
    """Return True when broad read/search navigation may inspect project code."""
    if not paths:
        if cwd is None:
            return True
        if _is_known_external_path(cwd):
            return False
        return project_root is None or _is_relative_to(cwd, project_root)

    for raw_path in paths:
        path = resolve_tool_path(raw_path, cwd)
        if path is None:
            return True
        if _is_known_external_path(path):
            continue
        if project_root is None or _is_relative_to(path, project_root):
            return True
    return False


def resolve_tool_path(path: Any, cwd: Path | None) -> Path | None:
    if not isinstance(path, str) or not path.strip():
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        if cwd is None:
            return None
        candidate = cwd / candidate
    return candidate.resolve(strict=False)


def _resolve_base_dir(path: Any) -> Path | None:
    if not isinstance(path, str) or not path.strip():
        return None
    return Path(path).expanduser().resolve(strict=False)


def _is_known_external_path(path: Path) -> bool:
    return _is_gobby_logs_path(path) or _is_temp_agent_scratchpad_path(path)


def _is_gobby_logs_path(path: Path) -> bool:
    logs_dir = (get_gobby_home().expanduser() / "logs").resolve(strict=False)
    return _is_relative_to(path, logs_dir)


def _is_temp_agent_scratchpad_path(path: Path) -> bool:
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    if not _is_relative_to(path, temp_root):
        return False

    for part in path.parts:
        marker = part.lower()
        if marker in _SCRATCHPAD_MARKERS:
            return True
        if marker.startswith(_SCRATCHPAD_PREFIXES):
            return True
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)
