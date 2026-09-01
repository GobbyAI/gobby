"""Build input resolution for task refs and plan files."""

from __future__ import annotations

from pathlib import Path

from gobby.build.options import BuildOptions
from gobby.build.stage_manifest import InputKind
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import (
    OverlayRegistrationRejectedError,
    require_root,
    resolve_operation_root,
)
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.workspace_machine_scope import require_local_machine_id
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
    candidates = plan_file_path_candidates(
        plan_file,
        project_root=_project_root_for_plan_lookup(task_manager, project_id),
    )
    if not candidates:
        return None
    registered = _open_registered_plan_file_build(task_manager, candidates, project_id)
    if registered is not None:
        return registered
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


def _open_registered_plan_file_build(
    task_manager: LocalTaskManager,
    candidates: tuple[str, ...],
    project_id: str,
) -> Task | None:
    placeholders = sql_placeholders(len(candidates))
    rows = task_manager.db.fetchall(
        f"""
        SELECT root_task_ref
          FROM plans
         WHERE project_id = %s
           AND state = 'active'
           AND plan_path IN ({placeholders})
         ORDER BY updated_at DESC, plan_id ASC
        """,  # nosec B608 # placeholder count is derived from normalized candidate paths.
        (project_id, *candidates),
    )
    for row in rows:
        try:
            task = task_manager.get_task(str(row["root_task_ref"]), project_id=project_id)
        except ValueError:
            continue
        if task.parent_task_id is None and task.task_type == "epic" and task.closed_at is None:
            return task
    return None


def plan_file_path_candidates(
    plan_file: Path,
    *,
    project_root: Path | None = None,
) -> tuple[str, ...]:
    candidates = [str(plan_file)]
    try:
        resolved = plan_file.resolve()
        candidates.append(str(resolved))
        if project_root is not None:
            candidates.append(str(resolved.relative_to(project_root.resolve())))
    except ValueError:
        pass
    except OSError:
        pass
    try:
        resolved = plan_file.resolve()
        for discovered_root in _project_roots_for_plan(resolved):
            candidates.append(str(resolved.relative_to(discovered_root)))
    except ValueError:
        pass
    except OSError:
        pass
    return tuple(dict.fromkeys(candidates))


def _local_machine_id(project_id: str) -> str:
    return require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)


def _checkout_root(db: HubDatabase, project_id: str) -> Path:
    return Path(require_root(db, project_id, _local_machine_id(project_id)))


def _project_root_for_plan_lookup(
    task_manager: LocalTaskManager,
    project_id: str,
) -> Path | None:
    return _checkout_root(task_manager.db, project_id)


def _project_roots_for_plan(plan_file: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    for parent in plan_file.parents:
        if parent.name == "plans" and parent.parent.name == ".gobby":
            roots.append(parent.parent.parent)
            break
    return tuple(roots)


def resolve_plan_file_path(
    input_ref: str,
    task_manager: LocalTaskManager,
    project_id: str,
    opts: BuildOptions,
) -> Path:
    path = Path(input_ref).expanduser()
    base_dir = plan_file_base_dir(task_manager, project_id, opts)
    direct_path = _resolve_under_base(path, base_dir, label="plan file")
    if direct_path.exists() or path.parent != Path("."):
        return direct_path
    plans_path = _resolve_under_base(
        Path(".gobby") / "plans" / path.name, base_dir, label="plan file"
    )
    if plans_path.exists():
        return plans_path
    return direct_path


def plan_file_base_dir(
    task_manager: LocalTaskManager,
    project_id: str,
    opts: BuildOptions,
) -> Path:
    machine_id = _local_machine_id(project_id)
    if opts.cwd is not None:
        overlay = str(opts.cwd.expanduser().resolve())
        try:
            return Path(
                resolve_operation_root(
                    task_manager.db, project_id, machine_id, overlay_path=overlay
                )
            )
        except OverlayRegistrationRejectedError:
            pass
    return Path(require_root(task_manager.db, project_id, machine_id))


def _resolve_under_base(path: Path, base_dir: Path, *, label: str) -> Path:
    base = base_dir.expanduser().resolve()
    candidate = path if path.is_absolute() else base / path
    resolved = candidate.expanduser().resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(f"{label} must stay inside {base}") from None
    return resolved


def looks_like_task_ref(input_ref: str) -> bool:
    return input_ref.startswith("#") or input_ref.isdigit()
