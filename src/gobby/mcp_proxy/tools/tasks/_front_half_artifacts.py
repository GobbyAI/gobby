"""Artifact path helpers for front-half orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import Task

INTERACTIVE_LOCK_PREFIX = "interactive:planning-in-progress:"


class ArtifactPaths(TypedDict):
    plan_file: str | None
    test_architecture_file: str


def artifact_paths(
    ctx: RegistryContext, parent_task: Task, stage_labels: dict[str, str]
) -> ArtifactPaths:
    ident = str(parent_task.seq_num) if parent_task.seq_num is not None else parent_task.id[:8]
    return {
        "plan_file": _persisted_plan_file(ctx, parent_task, stage_labels, ident),
        "test_architecture_file": f".gobby/test-architecture/task-{ident}-test-architecture.md",
    }


def _persisted_plan_file(
    ctx: RegistryContext, parent_task: Task, stage_labels: dict[str, str], ident: str
) -> str | None:
    task_plan_file = getattr(parent_task, "plan_file", None)
    if isinstance(task_plan_file, str) and task_plan_file.strip():
        return task_plan_file.strip()

    session_plan_file = _session_plan_file(ctx, parent_task, stage_labels, ident)
    if session_plan_file:
        return session_plan_file

    latest_run = LocalExpansionRunManager(ctx.task_manager.db).get_latest_for_task(parent_task.id)
    if latest_run and latest_run.plan_file:
        return latest_run.plan_file
    return _existing_plan_file(ctx, parent_task, ident)


def _session_plan_file(
    ctx: RegistryContext, parent_task: Task, stage_labels: dict[str, str], ident: str
) -> str | None:
    parent_refs = _parent_refs(parent_task)
    stage_child_ids = _stage_child_ids(ctx, parent_task, stage_labels)
    labels = set(parent_task.labels or [])
    rows = ctx.task_manager.db.fetchall(
        "SELECT session_id, variables FROM session_variables ORDER BY updated_at DESC"
    )
    for row in rows:
        variables = _load_variables(row["variables"])
        plan_file = variables.get("artifact_path")
        if not isinstance(plan_file, str) or not plan_file.strip():
            continue
        owns_lock = f"{INTERACTIVE_LOCK_PREFIX}{row['session_id']}" in labels
        if (
            owns_lock
            or _vars_point_to_parent(variables, parent_refs, stage_child_ids)
            or _plan_path_matches_ident(plan_file, ident)
        ):
            return plan_file.strip()
    return None


def _parent_refs(parent_task: Task) -> set[str]:
    refs = {parent_task.id}
    if parent_task.seq_num is not None:
        refs.update({f"#{parent_task.seq_num}", str(parent_task.seq_num)})
    if parent_task.path_cache:
        refs.add(parent_task.path_cache)
    return refs


def _stage_child_ids(
    ctx: RegistryContext, parent_task: Task, stage_labels: dict[str, str]
) -> set[str]:
    child_ids: set[str] = set()
    for label in stage_labels.values():
        tasks = ctx.task_manager.list_tasks(parent_task_id=parent_task.id, label=label, limit=200)
        child_ids.update(task.id for task in tasks)
    return child_ids


def _load_variables(raw: str | None) -> dict[str, Any]:
    try:
        loaded = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _vars_point_to_parent(
    variables: dict[str, Any], parent_refs: set[str], stage_child_ids: set[str]
) -> bool:
    refs = parent_refs | stage_child_ids
    for key in ("plan_parent_ref", "parent_task_id", "task_id", "assigned_task_id"):
        value = variables.get(key)
        if isinstance(value, str) and value in refs:
            return True
    claimed_tasks = variables.get("claimed_tasks")
    return isinstance(claimed_tasks, dict) and bool(
        refs.intersection(str(key) for key in claimed_tasks)
    )


def _plan_path_matches_ident(plan_file: str, ident: str) -> bool:
    normalized = plan_file.replace(chr(92), "/").strip()
    return (
        normalized.endswith(".md")
        and f"/task-{ident}-" in f"/{normalized}"
        and "/.gobby/plans/" in f"/{normalized}"
    )


def _existing_plan_file(ctx: RegistryContext, parent_task: Task, ident: str) -> str | None:
    repo_path = ctx.get_project_repo_path(parent_task.project_id)
    if not repo_path:
        return None
    plan_dir = Path(repo_path) / ".gobby" / "plans"
    if not plan_dir.is_dir():
        return None
    matches = [path for path in plan_dir.glob(f"task-{ident}-*.md") if path.is_file()]
    if not matches:
        return None
    newest = max(matches, key=lambda path: path.stat().st_mtime)
    return str(newest.relative_to(repo_path))
