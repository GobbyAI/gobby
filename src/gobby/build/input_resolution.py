"""Build input resolution for task refs and plan files."""

from __future__ import annotations

from pathlib import Path

from gobby.build.options import BuildOptions
from gobby.build.stage_manifest import InputKind
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.sql import sql_placeholders


def resolve_input(
    input_ref: str,
    task_manager: LocalTaskManager,
    project_id: str,
    opts: BuildOptions,
) -> tuple[InputKind, Task | Path]:
    if looks_like_task_ref(input_ref):
        task = task_manager.get_task(input_ref, project_id=project_id)
        return ("epic" if task.task_type == "epic" else "leaf", task)

    plan_file = resolve_plan_file_path(input_ref, task_manager, project_id, opts)
    if not plan_file.exists() or not plan_file.is_file():
        raise ValueError(f"plan file not found: {input_ref}")
    existing = open_plan_file_build(task_manager, plan_file, project_id)
    if existing is not None:
        return "epic", existing
    return "plan_file", plan_file


def open_plan_file_build(
    task_manager: LocalTaskManager,
    plan_file: Path,
    project_id: str,
) -> Task | None:
    candidates = plan_file_path_candidates(plan_file)
    if not candidates:
        return None
    placeholders = sql_placeholders(len(candidates))
    row = task_manager.db.fetchone(
        f"""
        SELECT t.id
          FROM tasks t
          JOIN task_artifacts a ON a.task_id = t.id
         WHERE t.project_id = %s
           AND t.parent_task_id IS NULL
           AND t.task_type = 'epic'
           AND t.closed_at IS NULL
           AND a.plan_file_path IN ({placeholders})
         ORDER BY t.created_at ASC
         LIMIT 1
        """,  # nosec B608 # placeholder count is derived from normalized candidate paths.
        (project_id, *candidates),
    )
    if row is None:
        return None
    return task_manager.get_task(str(row["id"]), project_id=project_id)


def plan_file_path_candidates(plan_file: Path) -> tuple[str, ...]:
    candidates = [str(plan_file)]
    try:
        candidates.append(str(plan_file.resolve()))
    except OSError:
        pass
    return tuple(dict.fromkeys(candidates))


def resolve_plan_file_path(
    input_ref: str,
    task_manager: LocalTaskManager,
    project_id: str,
    opts: BuildOptions,
) -> Path:
    path = Path(input_ref).expanduser()
    if path.is_absolute():
        return path
    base_dir = plan_file_base_dir(task_manager, project_id, opts)
    direct_path = base_dir / path
    if direct_path.exists() or path.parent != Path("."):
        return direct_path
    plans_path = base_dir / ".gobby" / "plans" / path.name
    if plans_path.exists():
        return plans_path
    return direct_path


def plan_file_base_dir(
    task_manager: LocalTaskManager,
    project_id: str,
    opts: BuildOptions,
) -> Path:
    if opts.cwd is not None:
        return opts.cwd.expanduser()
    project = LocalProjectManager(task_manager.db).get(project_id)
    if project is not None and project.repo_path:
        return Path(project.repo_path).expanduser()
    return Path.cwd()


def looks_like_task_ref(input_ref: str) -> bool:
    return input_ref.startswith("#") or input_ref.isdigit()
