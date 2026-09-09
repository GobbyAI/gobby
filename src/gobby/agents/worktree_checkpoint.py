"""Atomic Git checkpoints for terminal agent worktree recovery."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gobby.utils.daemon_git import GitOk, GitResult, daemon_git, parse_porcelain_v1_z
from gobby.workflows.task_claim_state import normalize_task_edited_path

CHECKPOINT_AUTHOR_NAME = "Gobby Coordinator"
CHECKPOINT_AUTHOR_EMAIL = "gobby-coordinator@localhost"
_GIT_TIMEOUT_SECONDS = 30


class WorktreeCheckpointError(RuntimeError):
    """A checkpoint could not be created without changing the checkout."""

    def __init__(self, message: str, *, error_code: str = "checkpoint_failed") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class WorktreeCheckpoint:
    """Committed recovery boundary for one terminal agent run."""

    commit_sha: str
    included_paths: tuple[str, ...]
    message: str


def checkpoint_commit_message(*, task_seq_num: int, run_id: str) -> str:
    """Return the stable task-tagged message for a recovery checkpoint."""
    return (
        f"[gobby-#{task_seq_num}] chore: checkpoint terminal agent worktree\n\nAgent-Run: {run_id}"
    )


async def checkpoint_worktree(
    *,
    worktree_path: str | Path,
    expected_paths: Collection[str],
    task_seq_num: int,
    run_id: str,
) -> WorktreeCheckpoint:
    """Commit the complete authorized dirty set without exposing partial staging state."""
    path = Path(worktree_path).resolve()
    normalized_expected = {
        normalized
        for item in expected_paths
        if (normalized := normalize_task_edited_path(item)) is not None
    }
    observed_paths = await _status_paths(path)
    if not observed_paths:
        raise WorktreeCheckpointError(
            "The terminal agent worktree is already clean",
            error_code="worktree_clean",
        )
    if observed_paths != normalized_expected:
        raise WorktreeCheckpointError(
            "The worktree dirty set changed after ownership validation",
            error_code="dirty_set_changed",
        )

    branch_ref = await _git_output(path, ["symbolic-ref", "--quiet", "HEAD"])
    old_head = await _git_output(path, ["rev-parse", "HEAD"])
    index_path = Path(
        await _git_output(path, ["rev-parse", "--path-format=absolute", "--git-path", "index"])
    )
    message = checkpoint_commit_message(task_seq_num=task_seq_num, run_id=run_id)

    with tempfile.TemporaryDirectory(prefix="gobby-checkpoint-", dir=index_path.parent) as temp_dir:
        temp_root = Path(temp_dir)
        alternate_index = temp_root / "index"
        index_backup = temp_root / "original-index"
        original_index_exists = index_path.exists()
        if original_index_exists:
            shutil.copy2(index_path, index_backup)

        alternate_env = {**os.environ, "GIT_INDEX_FILE": str(alternate_index)}
        await _git_checked(path, ["read-tree", old_head], env=alternate_env)
        await _git_checked(path, ["add", "-A", "--", "."], env=alternate_env)
        staged_paths = await _status_paths(path, env=alternate_env)
        if staged_paths != normalized_expected:
            raise WorktreeCheckpointError(
                "The staged checkpoint does not match the authorized dirty set",
                error_code="dirty_set_changed",
            )

        tree_sha = await _git_output(path, ["write-tree"], env=alternate_env)
        commit_env = {
            **alternate_env,
            "GIT_AUTHOR_NAME": CHECKPOINT_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": CHECKPOINT_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": CHECKPOINT_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": CHECKPOINT_AUTHOR_EMAIL,
        }
        commit_sha = await _git_output(
            path,
            ["commit-tree", tree_sha, "-p", old_head],
            env=commit_env,
            input_text=f"{message}\n",
        )

        ref_updated = False
        try:
            await _git_checked(path, ["update-ref", branch_ref, commit_sha, old_head])
            ref_updated = True
            await _git_checked(path, ["reset", "--mixed", "--quiet", commit_sha])
            remaining_paths = await _status_paths(path)
            if remaining_paths:
                raise WorktreeCheckpointError(
                    "The checkpoint commit did not leave the worktree clean",
                    error_code="checkpoint_not_clean",
                )
        except Exception as exc:
            rollback_error = await _rollback_checkpoint(
                path=path,
                branch_ref=branch_ref,
                old_head=old_head,
                commit_sha=commit_sha,
                ref_updated=ref_updated,
                index_path=index_path,
                index_backup=index_backup,
                original_index_exists=original_index_exists,
            )
            if rollback_error is not None:
                raise WorktreeCheckpointError(
                    f"Checkpoint failed and rollback failed: {rollback_error}",
                    error_code="checkpoint_rollback_failed",
                ) from exc
            if isinstance(exc, WorktreeCheckpointError):
                raise
            raise WorktreeCheckpointError(str(exc)) from exc

    return WorktreeCheckpoint(
        commit_sha=commit_sha,
        included_paths=tuple(sorted(normalized_expected)),
        message=message,
    )


async def _rollback_checkpoint(
    *,
    path: Path,
    branch_ref: str,
    old_head: str,
    commit_sha: str,
    ref_updated: bool,
    index_path: Path,
    index_backup: Path,
    original_index_exists: bool,
) -> str | None:
    errors: list[str] = []
    if ref_updated:
        result = await _run_git(path, ["update-ref", branch_ref, old_head, commit_sha])
        if not isinstance(result, GitOk):
            errors.append(_git_failure(result))
    try:
        if original_index_exists:
            os.replace(index_backup, index_path)
        else:
            index_path.unlink(missing_ok=True)
    except OSError as exc:
        errors.append(f"could not restore Git index: {exc}")
    return "; ".join(errors) or None


async def _status_paths(path: Path, *, env: Mapping[str, str] | None = None) -> set[str]:
    result = await daemon_git.status(path, timeout=_GIT_TIMEOUT_SECONDS, env=env)
    if not isinstance(result, GitOk):
        raise WorktreeCheckpointError(_git_failure(result))

    paths: set[str] = set()
    for entry in parse_porcelain_v1_z(result.stdout):
        normalized = normalize_task_edited_path(entry.path)
        if normalized is not None:
            paths.add(normalized)
        if entry.original_path is not None:
            original = normalize_task_edited_path(entry.original_path)
            if original is not None:
                paths.add(original)
    return paths


async def _git_output(
    path: Path,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    result = await _run_git(path, arguments, env=env, input_text=input_text)
    if not isinstance(result, GitOk):
        raise WorktreeCheckpointError(_git_failure(result))
    output = result.stdout.strip()
    if not output:
        raise WorktreeCheckpointError(f"git {arguments[0]} returned no output")
    return output


async def _git_checked(
    path: Path,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    result = await _run_git(path, arguments, env=env)
    if not isinstance(result, GitOk):
        raise WorktreeCheckpointError(_git_failure(result))


async def _run_git(
    path: Path,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> GitResult:
    return await daemon_git.run(
        arguments,
        cwd=path,
        env=env,
        input_text=input_text,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _git_failure(result: GitResult) -> str:
    if result.status == "timeout":
        detail = f"timed out after {result.timeout:g}s"
    else:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return f"git {result.argv[1]} failed: {detail}"
