"""Local Git hygiene for generated files in isolated agent workspaces."""

from __future__ import annotations

import json
import logging
import subprocess  # nosec B404 # fixed git argv for local workspace hygiene.
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MCP_CONFIG_RELATIVE_PATH = ".mcp.json"
PROJECT_JSON_RELATIVE_PATH = ".gobby/project.json"
GENERATED_ISOLATION_EXCLUDE_PATHS = (
    MCP_CONFIG_RELATIVE_PATH,
    ".claude/",
    ".codex/",
    ".factory/hooks/hooks.json",
    ".gobby/isolation.json",
)


def apply_isolation_git_hygiene(
    isolated_path: str | Path,
    *,
    main_repo_path: str | Path | None = None,
) -> None:
    """Hide Gobby-generated isolation metadata from local Git status.

    Exclude writes are only used when Git scopes ``info/exclude`` to this
    workspace. Linked worktrees share that file with the main checkout, so
    generated paths there are handled by reuse status filtering. Tracked
    project metadata is no longer skip-worktree'd; the isolation sidecar is
    gitignored instead.
    """
    _ = main_repo_path
    workspace = Path(isolated_path)
    if not workspace.is_dir():
        return

    for relative_path in GENERATED_ISOLATION_EXCLUDE_PATHS:
        _add_local_exclude(workspace, relative_path)
        _unstage_path(workspace, relative_path)
        if _git_path_is_tracked(workspace, relative_path):
            _mark_skip_worktree(workspace, relative_path)

    if not _git_path_is_tracked(workspace, PROJECT_JSON_RELATIVE_PATH):
        _add_local_exclude(workspace, PROJECT_JSON_RELATIVE_PATH)
        _unstage_path(workspace, PROJECT_JSON_RELATIVE_PATH)


def is_generated_isolation_project_json(
    project_json_path: str | Path,
    *,
    main_repo_path: str | Path | None,
) -> bool:
    """Return true when project metadata is Gobby-generated for an isolated root."""
    if main_repo_path is None:
        return False

    project_json = Path(project_json_path)
    data = _load_json_object(project_json)
    if data is None:
        return False

    parent_project_path = data.get("parent_project_path")
    parent_project_id = data.get("parent_project_id")
    project_id = data.get("id")
    if (
        not isinstance(parent_project_path, str)
        or not parent_project_path
        or not isinstance(parent_project_id, str)
        or not parent_project_id
        or not isinstance(project_id, str)
        or not project_id
    ):
        return False
    if not _same_path(Path(parent_project_path), Path(main_repo_path)):
        return False

    source_data = _load_json_object(Path(main_repo_path) / PROJECT_JSON_RELATIVE_PATH)
    source_project_id = source_data.get("id") if source_data else None
    if isinstance(source_project_id, str) and source_project_id:
        return parent_project_id == source_project_id and project_id == source_project_id
    return project_id == parent_project_id


def _load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)
    except OSError:
        return str(left.expanduser()) == str(right.expanduser())


def _add_local_exclude(workspace: Path, pattern: str) -> None:
    exclude_path = _git_info_exclude_path(workspace)
    if exclude_path is None:
        return
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        patterns = {line.strip() for line in existing.splitlines()}
        if pattern in patterns:
            return
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        exclude_path.write_text(f"{existing}{suffix}{pattern}\n", encoding="utf-8")
    except OSError:
        logger.debug("Failed to update Git exclude for %s in %s", pattern, workspace, exc_info=True)


def _git_info_exclude_path(workspace: Path) -> Path | None:
    if _is_linked_worktree(workspace):
        return None
    result = _run_git(workspace, ["rev-parse", "--git-path", "info/exclude"])
    if result.returncode != 0:
        return None
    raw_path = result.stdout.strip()
    if not raw_path:
        return None
    return _git_path_from_output(workspace, raw_path)


def _is_linked_worktree(workspace: Path) -> bool:
    git_dir = _rev_parse_git_path(workspace, "--git-dir")
    common_dir = _rev_parse_git_path(workspace, "--git-common-dir")
    if git_dir is None or common_dir is None:
        return False
    return not _same_path(git_dir, common_dir)


def _rev_parse_git_path(workspace: Path, arg: str) -> Path | None:
    result = _run_git(workspace, ["rev-parse", arg])
    if result.returncode != 0:
        return None
    raw_path = result.stdout.strip()
    if not raw_path:
        return None
    return _git_path_from_output(workspace, raw_path)


def _git_path_from_output(workspace: Path, raw_path: str) -> Path:
    parsed_path = Path(raw_path)
    return parsed_path if parsed_path.is_absolute() else workspace / parsed_path


def _git_path_is_tracked(workspace: Path, relative_path: str) -> bool:
    result = _run_git(workspace, ["ls-files", "--error-unmatch", "--", relative_path])
    return result.returncode == 0


def _unstage_path(workspace: Path, relative_path: str) -> None:
    _run_git(workspace, ["reset", "-q", "--", relative_path])


def _mark_skip_worktree(workspace: Path, relative_path: str) -> None:
    result = _run_git(workspace, ["update-index", "--skip-worktree", "--", relative_path])
    if result.returncode != 0:
        logger.debug(
            "Failed to mark %s skip-worktree in %s: %s",
            relative_path,
            workspace,
            result.stderr.strip() or result.stdout.strip(),
        )


def _run_git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # nosec B603 B607 # fixed git executable and argv.
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=1,
            stdout="",
            stderr=str(exc),
        )
