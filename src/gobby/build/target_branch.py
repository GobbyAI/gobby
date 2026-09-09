"""Target branch resolution for build automation."""

from __future__ import annotations

from pathlib import Path

from gobby.build.options import BuildOptions
from gobby.build.stage_manifest import InputKind
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import require_root
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workspace_machine_scope import require_local_machine_id
from gobby.utils.daemon_git import GitFailed, GitOk, GitTimeout, daemon_git


async def _resolve_target_branch(
    db: HubDatabase,
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
    db: HubDatabase,
    project_id: str,
    target_branch: str | None,
) -> None:
    if not target_branch:
        return
    repo_path = _checkout_root(db, project_id)
    if not (repo_path / ".git").exists():
        return

    result = await daemon_git.run(
        ["rev-parse", "--verify", target_branch],
        cwd=repo_path,
        timeout=10.0,
    )
    if isinstance(result, GitOk) and result.stdout.strip():
        return
    if isinstance(result, GitTimeout):
        raise RuntimeError(f"Git timed out validating target branch {target_branch}")
    if isinstance(result, GitFailed) and result.returncode is None:
        raise RuntimeError(f"Git unavailable while validating target branch: {result.stderr}")

    branches_result = await daemon_git.run(
        ["branch", "--format", "%(refname:short)"],
        cwd=repo_path,
        timeout=10.0,
    )
    if not isinstance(branches_result, GitOk):
        detail = (
            f"timed out after {branches_result.timeout:g}s"
            if isinstance(branches_result, GitTimeout)
            else branches_result.stderr.strip() or f"exit {branches_result.returncode}"
        )
        raise RuntimeError(f"Git unavailable while listing target branches: {detail}")
    available = ", ".join(branches_result.stdout.split()) or "main"
    raise ValueError(f"target branch {target_branch} is missing; available branches: {available}")


def _checkout_root(db: HubDatabase, project_id: str) -> Path:
    machine_id = require_local_machine_id(
        None, resource_kind="project_checkout", resource_id=project_id
    )
    return Path(require_root(db, project_id, machine_id))


async def _current_target_branch(db: HubDatabase, project_id: str) -> str | None:
    repo_path = _checkout_root(db, project_id)
    if not (repo_path / ".git").exists():
        return None

    result = await daemon_git.run(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path,
        timeout=10.0,
    )
    if not isinstance(result, GitOk):
        detail = (
            f"timed out after {result.timeout:g}s"
            if isinstance(result, GitTimeout)
            else result.stderr.strip() or f"exit {result.returncode}"
        )
        raise RuntimeError(f"Git unavailable while resolving the target branch: {detail}")
    branch = result.stdout.strip()
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
            WITH RECURSIVE subtree(id, depth, path) AS (
                SELECT id, 1, ARRAY[parent_task_id, id]
                FROM tasks
                WHERE parent_task_id = %s
                UNION ALL
                SELECT child.id, parent.depth + 1, parent.path || child.id
                FROM tasks child
                JOIN subtree parent ON child.parent_task_id = parent.id
                WHERE parent.depth < 100
                  AND NOT child.id = ANY(parent.path)
            )
            INSERT INTO task_artifacts (task_id, target_branch, updated_at)
            SELECT id, %s, CURRENT_TIMESTAMP
            FROM subtree
            WHERE id IS NOT NULL
            ON CONFLICT(task_id) DO UPDATE SET
                target_branch = excluded.target_branch,
                updated_at = CURRENT_TIMESTAMP
            """,
            (epic_id, target_branch),
        )
