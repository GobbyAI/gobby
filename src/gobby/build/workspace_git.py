"""Git helpers for build integration workspaces."""

from __future__ import annotations

import asyncio
import os
import subprocess  # nosec B404 # git subprocesses use fixed argument vectors.
from pathlib import Path

from gobby.build.workspace_common import BuildWorkspaceError
from gobby.paths import get_gobby_home
from gobby.utils.daemon_git import GitOk, GitTimeout, daemon_git
from gobby.utils.git import git_subprocess_env


def _workspace_path(kind: str, project_name: str, branch_name: str) -> Path:
    safe_branch = branch_name.replace("/", "-").replace("\\", "-")
    return get_gobby_home() / kind / project_name / safe_branch


async def _is_git_workspace_dir(path: str | Path) -> bool:
    workspace = Path(path)
    if not workspace.is_dir():
        return False
    result = await _git(workspace, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    return result.returncode == 0 and result.stdout.strip() == "true"


async def _branch_exists(repo_path: Path, branch_name: str) -> bool:
    result = await _git(repo_path, ["rev-parse", "--verify", branch_name], timeout=10)
    return result.returncode == 0


async def _ensure_source_branch(repo_path: Path, *, branch_name: str, base_branch: str) -> None:
    if await _branch_exists(repo_path, branch_name):
        return
    result = await _git(repo_path, ["branch", branch_name, base_branch], timeout=30)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BuildWorkspaceError(f"failed to create integration branch {branch_name}: {detail}")


async def _refresh_clean_git_dir(path: str | Path, branch_name: str, base_ref: str) -> None:
    workspace = Path(path)
    await _ensure_clean_git_dir(workspace)
    current = await _git(workspace, ["branch", "--show-current"], timeout=10)
    if current.returncode != 0:
        detail = current.stderr.strip() or current.stdout.strip()
        raise BuildWorkspaceError(f"failed to inspect integration branch {workspace}: {detail}")
    if current.stdout.strip() != branch_name:
        raise BuildWorkspaceError(
            f"integration workspace branch mismatch: {current.stdout.strip()} != {branch_name}"
        )

    if await _is_ancestor(workspace, base_ref, "HEAD"):
        return
    try:
        if await _is_ancestor(workspace, "HEAD", base_ref):
            result = await _git(workspace, ["merge", "--ff-only", base_ref], timeout=60)
        else:
            result = await _git(
                workspace,
                ["merge", "--no-edit", base_ref],
                timeout=60,
                env={"GOBBY_MERGE": "1"},
            )
    except asyncio.CancelledError:
        await _abort_merge_safely(workspace)
        raise
    except subprocess.TimeoutExpired as exc:
        await _abort_merge_safely(workspace)
        raise BuildWorkspaceError(
            f"failed to refresh integration workspace {workspace} from {base_ref}: "
            f"git merge timed out after {exc.timeout}s"
        ) from exc
    if result.returncode != 0:
        await _abort_merge_safely(workspace)
        detail = result.stderr.strip() or result.stdout.strip()
        raise BuildWorkspaceError(
            f"failed to refresh integration workspace {workspace} from {base_ref}: {detail}"
        )
    await _ensure_clean_git_dir(workspace)


async def _merge_required_commits(
    workspace: Path,
    *,
    commits: list[tuple[str, str]],
    source_repo_path: Path,
) -> None:
    await _ensure_clean_git_dir(workspace)
    for task_ref, commit_sha in commits:
        resolved_sha = await _ensure_commit_available(workspace, commit_sha, source_repo_path)
        if await _is_ancestor(workspace, resolved_sha, "HEAD"):
            continue
        try:
            result = await _git(
                workspace,
                ["merge", "--no-ff", "--no-edit", resolved_sha],
                timeout=120,
                env={"GOBBY_MERGE": "1"},
            )
        except asyncio.CancelledError:
            await _abort_merge_safely(workspace)
            raise
        except subprocess.TimeoutExpired as exc:
            await _abort_merge_safely(workspace)
            raise BuildWorkspaceError(
                f"failed to merge closed child commit {commit_sha} from {task_ref}: "
                f"git merge timed out after {exc.timeout}s"
            ) from exc
        if result.returncode != 0:
            await _abort_merge_safely(workspace)
            detail = result.stderr.strip() or result.stdout.strip()
            raise BuildWorkspaceError(
                f"failed to merge closed child commit {commit_sha} from {task_ref}: {detail}"
            )
        await _ensure_clean_git_dir(workspace)


async def _ensure_commit_available(
    workspace: Path,
    commit_sha: str,
    source_repo_path: Path,
) -> str:
    resolved = await _resolve_commit(workspace, commit_sha)
    if resolved:
        return resolved

    direct_fetch = await _git(workspace, ["fetch", str(source_repo_path), commit_sha], timeout=60)
    resolved = await _resolve_commit(workspace, commit_sha)
    if resolved:
        return resolved

    branch_fetch = await _git(
        workspace,
        ["fetch", str(source_repo_path), "+refs/heads/*:refs/remotes/gobby-source/*"],
        timeout=120,
    )
    resolved = await _resolve_commit(workspace, commit_sha)
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


async def _resolve_commit(workspace: Path, commit_sha: str) -> str | None:
    result = await _git(
        workspace, ["rev-parse", "--verify", f"{commit_sha}^{{commit}}"], timeout=10
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


async def _abort_merge_safely(workspace: Path) -> None:
    try:
        await _git(workspace, ["merge", "--abort"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


async def _is_ancestor(repo_path: Path, ancestor: str, descendant: str) -> bool:
    result = await _git(
        repo_path,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        timeout=30,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
    raise BuildWorkspaceError(
        f"failed to compare {ancestor} with {descendant} in {repo_path}: {detail}"
    )


async def _clone_base_ref(path: str | Path, base_branch: str) -> str:
    workspace = Path(path)
    fetch = await _git(
        workspace,
        ["fetch", "origin", f"{base_branch}:refs/remotes/origin/{base_branch}"],
        timeout=60,
    )
    if fetch.returncode == 0:
        remote_ref = f"origin/{base_branch}"
        if (
            await _git(workspace, ["rev-parse", "--verify", remote_ref], timeout=10)
        ).returncode == 0:
            return remote_ref
    return base_branch


async def _ensure_clean_git_dir(path: str | Path) -> None:
    result = await _git(Path(path), ["status", "--porcelain"], timeout=10)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BuildWorkspaceError(f"failed to inspect integration workspace {path}: {detail}")
    if result.stdout.strip():
        raise BuildWorkspaceError(f"integration workspace is dirty; clean/restart: {path}")


async def _git(
    repo_path: Path,
    args: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    subprocess_env = git_subprocess_env()
    if env is not None:
        subprocess_env = {**(subprocess_env if subprocess_env is not None else os.environ), **env}
    result = await daemon_git.run(
        args,
        cwd=repo_path,
        timeout=timeout,
        env=subprocess_env,
    )
    if isinstance(result, GitTimeout):
        raise subprocess.TimeoutExpired(
            result.argv,
            result.timeout,
            output=result.stdout,
            stderr=result.stderr,
        )
    if result.returncode is None:
        raise OSError(result.stderr or "Git could not be started")
    return subprocess.CompletedProcess(
        args=result.argv,
        returncode=0 if isinstance(result, GitOk) else result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
