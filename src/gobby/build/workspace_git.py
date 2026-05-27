"""Git helpers for build integration workspaces."""

from __future__ import annotations

import os
import subprocess  # nosec B404 # git subprocesses use fixed argument vectors.
from pathlib import Path

from gobby.build.workspace_common import BuildWorkspaceError


def _workspace_path(kind: str, project_name: str, branch_name: str) -> Path:
    safe_branch = branch_name.replace("/", "-").replace("\\", "-")
    return Path.home() / ".gobby" / kind / project_name / safe_branch


def _branch_exists(repo_path: Path, branch_name: str) -> bool:
    result = _git(repo_path, ["rev-parse", "--verify", branch_name], timeout=10)
    return result.returncode == 0


def _ensure_source_branch(repo_path: Path, *, branch_name: str, base_branch: str) -> None:
    if _branch_exists(repo_path, branch_name):
        return
    result = _git(repo_path, ["branch", branch_name, base_branch], timeout=30)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BuildWorkspaceError(f"failed to create integration branch {branch_name}: {detail}")


def _refresh_clean_git_dir(path: str | Path, branch_name: str, base_ref: str) -> None:
    workspace = Path(path)
    _ensure_clean_git_dir(workspace)
    current = _git(workspace, ["branch", "--show-current"], timeout=10)
    if current.returncode != 0:
        detail = current.stderr.strip() or current.stdout.strip()
        raise BuildWorkspaceError(f"failed to inspect integration branch {workspace}: {detail}")
    if current.stdout.strip() != branch_name:
        raise BuildWorkspaceError(
            f"integration workspace branch mismatch: {current.stdout.strip()} != {branch_name}"
        )

    if _is_ancestor(workspace, base_ref, "HEAD"):
        return
    try:
        if _is_ancestor(workspace, "HEAD", base_ref):
            result = _git(workspace, ["merge", "--ff-only", base_ref], timeout=60)
        else:
            result = _git(
                workspace,
                ["merge", "--no-edit", base_ref],
                timeout=60,
                env={"GOBBY_MERGE": "1"},
            )
    except subprocess.TimeoutExpired as exc:
        _abort_merge_safely(workspace)
        raise BuildWorkspaceError(
            f"failed to refresh integration workspace {workspace} from {base_ref}: "
            f"git merge timed out after {exc.timeout}s"
        ) from exc
    if result.returncode != 0:
        _abort_merge_safely(workspace)
        detail = result.stderr.strip() or result.stdout.strip()
        raise BuildWorkspaceError(
            f"failed to refresh integration workspace {workspace} from {base_ref}: {detail}"
        )
    _ensure_clean_git_dir(workspace)


def _merge_required_commits(
    workspace: Path,
    *,
    commits: list[tuple[str, str]],
    source_repo_path: Path,
) -> None:
    _ensure_clean_git_dir(workspace)
    for task_ref, commit_sha in commits:
        resolved_sha = _ensure_commit_available(workspace, commit_sha, source_repo_path)
        if _is_ancestor(workspace, resolved_sha, "HEAD"):
            continue
        try:
            result = _git(
                workspace,
                ["merge", "--no-ff", "--no-edit", resolved_sha],
                timeout=120,
                env={"GOBBY_MERGE": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            _abort_merge_safely(workspace)
            raise BuildWorkspaceError(
                f"failed to merge closed child commit {commit_sha} from {task_ref}: "
                f"git merge timed out after {exc.timeout}s"
            ) from exc
        if result.returncode != 0:
            _abort_merge_safely(workspace)
            detail = result.stderr.strip() or result.stdout.strip()
            raise BuildWorkspaceError(
                f"failed to merge closed child commit {commit_sha} from {task_ref}: {detail}"
            )
        _ensure_clean_git_dir(workspace)


def _ensure_commit_available(
    workspace: Path,
    commit_sha: str,
    source_repo_path: Path,
) -> str:
    resolved = _resolve_commit(workspace, commit_sha)
    if resolved:
        return resolved

    direct_fetch = _git(workspace, ["fetch", str(source_repo_path), commit_sha], timeout=60)
    resolved = _resolve_commit(workspace, commit_sha)
    if resolved:
        return resolved

    branch_fetch = _git(
        workspace,
        ["fetch", str(source_repo_path), "+refs/heads/*:refs/remotes/gobby-source/*"],
        timeout=120,
    )
    resolved = _resolve_commit(workspace, commit_sha)
    if resolved:
        return resolved

    detail = (
        direct_fetch.stderr.strip()
        or branch_fetch.stderr.strip()
        or direct_fetch.stdout.strip()
        or branch_fetch.stdout.strip()
        or "commit not found"
    )
    raise BuildWorkspaceError(f"closed child commit {commit_sha} is unavailable: {detail}")


def _resolve_commit(workspace: Path, commit_sha: str) -> str | None:
    result = _git(workspace, ["rev-parse", "--verify", f"{commit_sha}^{{commit}}"], timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _abort_merge_safely(workspace: Path) -> None:
    try:
        _git(workspace, ["merge", "--abort"], timeout=30)
    except subprocess.TimeoutExpired:
        pass


def _is_ancestor(repo_path: Path, ancestor: str, descendant: str) -> bool:
    result = _git(
        repo_path,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        timeout=30,
    )
    return result.returncode == 0


def _clone_base_ref(path: str | Path, base_branch: str) -> str:
    workspace = Path(path)
    fetch = _git(
        workspace,
        ["fetch", "origin", f"{base_branch}:refs/remotes/origin/{base_branch}"],
        timeout=60,
    )
    if fetch.returncode == 0:
        remote_ref = f"origin/{base_branch}"
        if _git(workspace, ["rev-parse", "--verify", remote_ref], timeout=10).returncode == 0:
            return remote_ref
    return base_branch


def _ensure_clean_git_dir(path: str | Path) -> None:
    result = _git(Path(path), ["status", "--porcelain"], timeout=10)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BuildWorkspaceError(f"failed to inspect integration workspace {path}: {detail}")
    if result.stdout.strip():
        raise BuildWorkspaceError(f"integration workspace is dirty; clean/restart: {path}")


def _git(
    repo_path: Path,
    args: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 # git args are fixed by callers.
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **env} if env is not None else None,
        check=False,
    )
