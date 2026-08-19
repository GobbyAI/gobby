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
# CLI-host scratchpads (e.g. Claude Code's /private/tmp/claude-<uid>/...) live
# under system temp roots that differ from the daemon's tempfile.gettempdir().
_EXTRA_TEMP_ROOTS = ("/tmp", "/private/tmp")
# AI-CLI state homes (transcripts, session stores, settings) are not project
# code; navigation there must not trip code-index preference rules. Project
# worktrees checked out beneath these roots still classify as in-project
# because project-root membership is checked first.
_AGENT_STATE_HOME_DIRS = (
    ".claude",
    ".codex",
    ".factory",
    ".gemini",
    ".grok",
    ".qwen",
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
        scope_unknown = bool(metadata.pop("_canonical_repo_mutation_scope_unknown", False))
        metadata["canonical_repo_mutation"] = scope_unknown or paths_may_touch_project(
            paths, cwd=cwd, project_root=project_root
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
    """Return True when broad read/search navigation may inspect project code.

    In-project membership wins over the known-external roots so a project
    checked out beneath an external root (a worktree under a CLI state home)
    still counts as project navigation.
    """
    if not paths:
        if cwd is None:
            return True
        if project_root is not None and _is_relative_to(cwd, project_root):
            return True
        if _is_known_external_path(cwd):
            return False
        return project_root is None

    for raw_path in paths:
        path = resolve_tool_path(raw_path, cwd)
        if path is None:
            return True
        if project_root is not None and _is_relative_to(path, project_root):
            return True
        if _is_known_external_path(path):
            continue
        if project_root is None:
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
    return (
        _is_gobby_home_path(path)
        or _is_agent_state_home_path(path)
        or _is_temp_agent_scratchpad_path(path)
    )


def _is_gobby_home_path(path: Path) -> bool:
    gobby_home = get_gobby_home().expanduser().resolve(strict=False)
    return _is_relative_to(path, gobby_home)


def _is_agent_state_home_path(path: Path) -> bool:
    """Return True for paths under an AI-CLI state home such as ``~/.claude``."""
    home = Path.home().resolve(strict=False)
    return any(_is_relative_to(path, home / name) for name in _AGENT_STATE_HOME_DIRS)


def _temp_scratchpad_roots() -> frozenset[Path]:
    roots = {Path(tempfile.gettempdir()), *(Path(raw) for raw in _EXTRA_TEMP_ROOTS)}
    return frozenset(root.resolve(strict=False) for root in roots)


def _is_temp_agent_scratchpad_path(path: Path) -> bool:
    if not any(_is_relative_to(path, root) for root in _temp_scratchpad_roots()):
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
