"""Shared Git helpers for landing onto a target checkout with a dirty index."""

from __future__ import annotations

import subprocess  # nosec B404 # CompletedProcess typing for fixed git commands.
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from gobby.worktrees.git._lifecycle import sync_from_main
from gobby.worktrees.git._models import GitOperationResult
from gobby.worktrees.git._runner import GitRunner

MERGE_ENV = {"GOBBY_MERGE": "1"}


class GitCommandRunner(Protocol):
    """Git command surface shared by worktree and clone managers."""

    def run_git_command(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 30,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class FastForwardLandingResult:
    """Outcome of syncing a source branch and fast-forwarding its target."""

    success: bool
    step: str | None = None
    error: str | None = None
    conflicted_files: tuple[str, ...] = ()
    output: str = ""


def _status_path_is_gobby_only(pathspec: str) -> bool:
    paths = [part.strip() for part in pathspec.split(" -> ")]
    return all(path == ".gobby" or path.startswith(".gobby/") for path in paths)


def _porcelain_pathspec(line: str) -> str:
    if len(line) >= 3 and line[2] == " ":
        return line[3:]
    if len(line) >= 2 and line[1] == " ":
        return line[2:]
    return line[3:] if len(line) > 3 else line


def _non_gobby_status_lines(status_output: str) -> list[str]:
    dirty: list[str] = []
    for line in status_output.splitlines():
        if not line:
            continue
        pathspec = _porcelain_pathspec(line)
        if not _status_path_is_gobby_only(pathspec):
            dirty.append(line)
    return dirty


def _non_gobby_dirty_paths(status_output: str) -> set[str]:
    paths: set[str] = set()
    for line in _non_gobby_status_lines(status_output):
        pathspec = _porcelain_pathspec(line)
        paths.update(part.strip() for part in pathspec.split(" -> ") if part.strip())
    return paths


def staged_paths(runner: GitCommandRunner, cwd: str | Path) -> set[str]:
    """Return staged paths outside .gobby/ for one checkout."""
    result = runner.run_git_command(
        ["diff", "--name-only", "--cached"],
        cwd=cwd,
        timeout=10,
    )
    if result.returncode != 0:
        detail = result.stderr or result.stdout or f"git exited with status {result.returncode}"
        raise RuntimeError(f"Failed to inspect staged target paths: {detail.strip()}")
    return {
        path.strip()
        for path in result.stdout.splitlines()
        if path.strip() and not _status_path_is_gobby_only(path.strip())
    }


def _qualified_branch_ref(ref: str, label: str) -> str:
    if not ref.startswith("refs/heads/"):
        raise ValueError(f"{label} must be a qualified local branch ref: {ref}")
    return ref


def _sync_source_branch(
    runner: GitCommandRunner,
    source_cwd: str | Path,
    target_ref: str,
) -> GitOperationResult:
    sync_method = getattr(runner, "sync_from_main", None)
    if callable(sync_method):
        return cast(
            GitOperationResult,
            sync_method(
                source_cwd,
                strategy="merge",
                source_branch=target_ref,
                env=MERGE_ENV,
            ),
        )
    return sync_from_main(
        cast(GitRunner, runner),
        source_cwd,
        strategy="merge",
        source_branch=target_ref,
        env=MERGE_ENV,
    )


def land_by_fast_forward(
    runner: GitCommandRunner,
    *,
    source_cwd: str | Path,
    target_cwd: str | Path,
    source_ref: str,
    target_ref: str,
    landing_ref: str | None = None,
    separate_repositories: bool = False,
) -> FastForwardLandingResult:
    """Merge target into source, then fast-forward target without touching unrelated paths."""
    source_ref = _qualified_branch_ref(source_ref, "source_ref")
    target_ref = _qualified_branch_ref(target_ref, "target_ref")
    effective_landing_ref = _qualified_branch_ref(landing_ref or source_ref, "landing_ref")

    if separate_repositories:
        fetch_target = runner.run_git_command(
            ["fetch", str(target_cwd), f"+{target_ref}:{target_ref}"],
            cwd=source_cwd,
            timeout=120,
        )
        if fetch_target.returncode != 0:
            detail = fetch_target.stderr or fetch_target.stdout
            return FastForwardLandingResult(
                success=False,
                step="sync-into-branch",
                error=f"Failed to fetch target branch into source: {detail.strip()}",
            )

    sync_result = _sync_source_branch(runner, source_cwd, target_ref)
    if not sync_result.success:
        conflicts = tuple(
            path.strip() for path in (sync_result.output or "").splitlines() if path.strip()
        )
        return FastForwardLandingResult(
            success=False,
            step="sync-into-branch",
            error=sync_result.error or sync_result.message,
            conflicted_files=conflicts,
        )

    if separate_repositories:
        fetch_source = runner.run_git_command(
            ["fetch", str(source_cwd), f"+{source_ref}:{effective_landing_ref}"],
            cwd=target_cwd,
            timeout=120,
        )
        if fetch_source.returncode != 0:
            detail = fetch_source.stderr or fetch_source.stdout
            return FastForwardLandingResult(
                success=False,
                step="fast-forward",
                error=f"Failed to refresh source branch for landing: {detail.strip()}",
            )

    merge_result = runner.run_git_command(
        ["merge", "--ff-only", effective_landing_ref],
        cwd=target_cwd,
        timeout=240,
        env=MERGE_ENV,
    )
    if merge_result.returncode != 0:
        detail = merge_result.stderr or merge_result.stdout
        return FastForwardLandingResult(
            success=False,
            step="fast-forward",
            error=detail.strip(),
        )
    return FastForwardLandingResult(
        success=True,
        output=(merge_result.stdout or merge_result.stderr).strip(),
    )
