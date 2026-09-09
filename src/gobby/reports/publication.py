"""Verify Git evidence before marking a synthesis publication complete."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gobby.reports.storage import ReportStore, transient_publication_error
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.daemon_git import GitFailed, GitOk, GitTimeout, daemon_git


async def verify_publication(
    store: ReportStore, report: dict[str, Any], repo_path: Path, attempt_id: str
) -> str:
    """Use committed bytes and durable task/worktree linkage, never agent assertions."""
    if not report.get("content") or not report.get("content_hash"):
        raise ValueError("Reporter did not persist a validated draft before publication")
    branch = str(report["branch_name"])
    if not branch.startswith(f"reports/{report['source_kind']}/"):
        raise ValueError("Publication branch does not belong to the report")

    async def git(*args: str, consume: Callable[[bytes], object]) -> None:
        result = await daemon_git.stream_bytes(
            args,
            cwd=repo_path,
            consume=consume,
            timeout=30.0,
        )
        if isinstance(result, GitTimeout):
            raise OSError(f"Git command timed out after {result.timeout:g}s: {result.argv!r}")
        if isinstance(result, GitFailed):
            error = result.stderr.strip()
            if transient_publication_error(error):
                raise OSError(error)
            raise ValueError(error)
        if not isinstance(result, GitOk):
            raise OSError("Git service returned no usable publication result")

    commit_bytes = bytearray()
    await git(
        "rev-parse",
        "--verify",
        f"refs/heads/{branch}^{{commit}}",
        consume=commit_bytes.extend,
    )
    commit = bytes(commit_bytes).decode().strip()
    content_hash = hashlib.sha256()
    await git("show", f"{commit}:{report['report_path']}", consume=content_hash.update)
    if content_hash.hexdigest() != report["content_hash"]:
        raise ValueError("Committed report content does not match the persisted draft hash")
    return await asyncio.to_thread(
        _complete_publication,
        store,
        report,
        attempt_id,
        branch,
        commit,
    )


def _complete_publication(
    store: ReportStore,
    report: dict[str, Any],
    attempt_id: str,
    branch: str,
    commit: str,
) -> str:
    """Validate durable linkage and commit the publication transaction off-loop."""
    task = LocalTaskManager(store.db).get_task(str(report["task_id"]))
    if task.closed_at is None or commit not in (task.commits or []):
        raise ValueError("Publication commit must be linked to the closed documentation task")
    worktree = store.db.fetchone(
        "SELECT task_id, branch_name FROM worktrees WHERE id = %s", (report["worktree_id"],)
    )
    if (
        worktree is None
        or str(worktree["task_id"]) != str(task.id)
        or worktree["branch_name"] != branch
    ):
        raise ValueError("Publication worktree is not linked to the report task and branch")
    with store.db.transaction() as conn:
        attempt = conn.execute(
            """UPDATE synthesis_report_attempts SET phase = 'verification', status = 'completed', completed_at = now()
            WHERE id = %s AND source_kind = %s AND source_run_id = %s AND status = 'running' RETURNING id""",
            (attempt_id, report["source_kind"], report["source_run_id"]),
        ).fetchone()
        if attempt is None:
            raise ValueError("Publication attempt is no longer active")
        updated = conn.execute(
            """UPDATE synthesis_reports SET status = 'completed', commit_sha = %s, updated_at = now()
            WHERE source_kind = %s AND source_run_id = %s AND content_hash = %s""",
            (commit, report["source_kind"], report["source_run_id"], report["content_hash"]),
        )
        if updated.rowcount != 1:
            raise ValueError("Report draft changed during publication verification")
    return commit
