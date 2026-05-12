"""Target branch resolution for build automation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from gobby.build.options import BuildOptions
from gobby.build.stage_manifest import InputKind
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager


async def _resolve_target_branch(
    db: DatabaseProtocol,
    project_id: str,
    opts: BuildOptions,
    input_kind: InputKind,
) -> str | None:
    if opts.target_branch:
        await _validate_target_branch(db, project_id, opts.target_branch)
        return opts.target_branch
    if input_kind == "leaf" and opts.isolation == "none":
        return None
    return await _current_target_branch(db, project_id)


async def _validate_target_branch(
    db: DatabaseProtocol,
    project_id: str,
    target_branch: str | None,
) -> None:
    if not target_branch:
        return
    project = LocalProjectManager(db).get(project_id)
    if project is None or project.repo_path is None:
        return
    repo_path = Path(project.repo_path)
    if not (repo_path / ".git").exists():
        return

    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--verify",
        target_branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode == 0 and stdout_bytes.decode().strip():
        return

    list_proc = await asyncio.create_subprocess_exec(
        "git",
        "branch",
        "--format",
        "%(refname:short)",
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    branches_stdout, _ = await list_proc.communicate()
    available = ", ".join(branches_stdout.decode().split()) or "main"
    raise ValueError(f"target branch {target_branch} is missing; available branches: {available}")


async def _current_target_branch(db: DatabaseProtocol, project_id: str) -> str | None:
    project = LocalProjectManager(db).get(project_id)
    if project is None or project.repo_path is None:
        return None
    repo_path = Path(project.repo_path)
    if not (repo_path / ".git").exists():
        return None

    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--abbrev-ref",
        "HEAD",
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    branch = stdout_bytes.decode().strip()
    return branch or None


def _cascade_target_branch_to_subtree(
    task_manager: LocalTaskManager,
    epic_id: str,
    target_branch: str | None,
) -> None:
    if not target_branch:
        return
    with task_manager.db.transaction() as conn:
        conn.execute(
            """
            WITH RECURSIVE subtree(id) AS (
                SELECT id
                FROM tasks
                WHERE parent_task_id = ?
                UNION ALL
                SELECT child.id
                FROM tasks child
                JOIN subtree parent ON child.parent_task_id = parent.id
            )
            INSERT INTO task_artifacts (task_id, target_branch, updated_at)
            SELECT id, ?, datetime('now')
            FROM subtree
            WHERE id IS NOT NULL
            ON CONFLICT(task_id) DO UPDATE SET
                target_branch = excluded.target_branch,
                updated_at = datetime('now')
            """,
            (epic_id, target_branch),
        )
