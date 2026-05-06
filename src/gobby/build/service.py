"""Shared build service for lifecycle automation entry points."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from gobby.build.dispatch_tick import (
    DispatcherTickSummary,
)
from gobby.build.dispatch_tick import (
    kick_dispatcher_tick as _kick_dispatcher_tick,
)
from gobby.build.options import BuildOptions, retry_attempt_cap
from gobby.build.workspaces import ensure_epic_integration_workspaces
from gobby.config.build import Isolation
from gobby.runner import install_dispatcher_cron_row
from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import (
    LocalTaskManager,
    ManifestAlreadyInitializedError,
    StageManifestSpec,
    Task,
    TaskArtifacts,
)

logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    task_id: str
    created: bool
    initial_lifecycle: str
    applied_stages_skipped: list[str]
    tick_dispatched: int
    dispatcher_tick: DispatcherTickSummary = field(default_factory=DispatcherTickSummary)
    manifest: list[dict[str, str | int | None]] | None = None

    @property
    def stage_manifest(self) -> list[dict[str, str | int | None]] | None:
        return self.manifest


@dataclass(frozen=True)
class BuildLifecycleEvent:
    """Project-level build lifecycle audit event."""

    id: int
    project_id: str
    event: str
    reason: str
    by_actor: str
    created_at: str


@dataclass(frozen=True)
class BuildControlResult:
    """Result returned by build stop/resume entry points."""

    project_id: str
    enabled: bool
    cron_job_id: str
    lifecycle_event: BuildLifecycleEvent


DEVELOPMENT_LEAF_CATEGORIES = frozenset({"code", "config", "docs", "refactor", "test"})
LEAF_PRIMARY_STAGE_BY_CATEGORY = {
    "code": "development",
    "config": "development",
    "docs": "development",
    "refactor": "development",
    "test": "development",
    "research": "research",
    "planning": "planning",
}
AUTOMATED_LEAF_CATEGORIES = frozenset(LEAF_PRIMARY_STAGE_BY_CATEGORY)
InputKind = Literal["plan_file", "epic", "leaf"]

_SKIPPABLE_STAGE_ORDER = (
    "plan_review",
    "expanding",
    "qa",
    "holistic_review",
    "pr",
)
_CANONICAL_STAGE_NAMES = {
    "ideation",
    "research",
    "architecture",
    "prd",
    "planning",
    "expansion",
    "development",
    "holistic_qa",
    "pr",
    "merge",
}
_LEGACY_STAGE_ALIASES: dict[str, str | None] = {
    "plan_review": "planning",
    "expanding": "expansion",
    "holistic_review": "holistic_qa",
    "qa": None,
}


async def build(
    input_ref: str,
    opts: BuildOptions,
    *,
    db: DatabaseProtocol,
    project_id: str,
    services: object | None = None,
) -> BuildResult:
    """Start lifecycle automation for a plan file, epic, or automated leaf task."""

    skip_stages = _validate_skip_stages(opts.skip_stages)
    task_manager = LocalTaskManager(db)
    input_kind, task_or_plan = _resolve_input(input_ref, task_manager, project_id)

    _validate_no_merge(opts)
    _validate_clones_dir(opts)
    _validate_retry_caps(opts)
    _validate_max_active_agents(opts)
    target_branch = await _resolve_target_branch(db, project_id, opts, input_kind)

    if input_kind == "plan_file":
        assert isinstance(task_or_plan, Path)
        return await _build_plan_file(
            task_manager,
            task_or_plan,
            opts,
            skip_stages,
            project_id,
            target_branch,
            services,
        )

    assert isinstance(task_or_plan, Task)
    task = task_or_plan
    if task_manager.stage_states.list_for_task(task.id):
        return await _resume_existing_lifecycle(
            task_manager,
            task,
            opts,
            skip_stages,
            db,
            project_id,
            services,
            target_branch,
        )

    _prepare_task_ref_expansion_output(task_manager, task, opts)
    if input_kind == "leaf":
        return await _build_leaf(
            task_manager,
            task,
            opts,
            skip_stages,
            target_branch,
            db,
            project_id,
            services,
        )

    return await _build_epic(
        task_manager,
        task,
        opts,
        skip_stages,
        target_branch,
        db,
        project_id,
        services,
    )


async def _build_plan_file(
    task_manager: LocalTaskManager,
    plan_file: Path,
    opts: BuildOptions,
    skip_stages: list[str],
    project_id: str,
    target_branch: str | None,
    services: object | None,
) -> BuildResult:
    task = task_manager.create_task(
        project_id=project_id,
        title=f"Build {plan_file.name}",
        description=f"Lifecycle automation seeded from plan file: {plan_file}",
        task_type="epic",
        category="planning",
    )
    task_manager.update_task(
        task.id,
        allow_automation=True,
        unattended=False,
        isolation=opts.isolation,
        assigned_agent=opts.assigned_agent,
    )
    task_manager.artifacts.set_artifacts_atomic(
        task.id,
        plan_file_path=str(plan_file),
        target_branch=target_branch,
    )
    specs = _initialize_stage_manifest(task_manager, task, opts, skip_stages, "plan_file")
    initial_lifecycle = _current_stage_name(task_manager, task.id, specs)
    _record_build_event(task_manager, task.id, initial_lifecycle)
    tick = await _kick_dispatcher_tick(
        task_manager.db,
        project_id,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_active_agents=opts.max_active_agents,
    )
    if opts.quick:
        _set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
    return BuildResult(
        task_id=task.id,
        created=True,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=skip_stages,
        tick_dispatched=tick.ticks,
        dispatcher_tick=tick,
        manifest=_specs_payload(specs),
    )


async def _build_leaf(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    target_branch: str | None,
    db: DatabaseProtocol,
    project_id: str,
    services: object | None,
) -> BuildResult:
    if task.category not in AUTOMATED_LEAF_CATEGORIES:
        if task.category == "manual":
            raise ValueError("manual leaf tasks are not automatable")
        allowed = ", ".join(sorted(AUTOMATED_LEAF_CATEGORIES))
        raise ValueError(
            f"category {task.category} cannot be automated; expected one of: {allowed}"
        )
    _validate_task_ref_isolation_artifacts(task_manager, task, opts.isolation)

    task_manager.update_task(
        task.id,
        allow_automation=True,
        unattended=False,
        isolation=opts.isolation,
        assigned_agent=opts.assigned_agent,
    )
    if opts.isolation in {"worktree", "clone"} and target_branch:
        task_manager.artifacts.set_artifact(task.id, "target_branch", target_branch)
    specs = _initialize_stage_manifest(task_manager, task, opts, skip_stages, "leaf")
    initial_lifecycle = _current_stage_name(task_manager, task.id, specs)
    _record_build_event(task_manager, task.id, initial_lifecycle)
    tick = await _kick_dispatcher_tick(
        db,
        project_id,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_active_agents=opts.max_active_agents,
    )
    if opts.quick:
        _set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
    return BuildResult(
        task_id=task.id,
        created=False,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=skip_stages,
        tick_dispatched=tick.ticks,
        dispatcher_tick=tick,
        manifest=_specs_payload(specs),
    )


async def _build_epic(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    target_branch: str | None,
    db: DatabaseProtocol,
    project_id: str,
    services: object | None,
) -> BuildResult:
    artifacts = task_manager.artifacts.get_artifacts(task.id)
    _validate_epic_isolation_artifacts(opts.isolation, artifacts)
    task_manager.artifacts.set_artifacts_atomic(
        task.id,
        target_branch=target_branch,
    )
    if opts.isolation in {"worktree", "clone"}:
        if target_branch is None:
            raise ValueError("target_branch is required for epic integration workspaces")
        ensure_epic_integration_workspaces(
            task_manager=task_manager,
            root_task=task,
            backend=opts.workspace_backend,
            target_branch=target_branch,
            project_id=project_id,
            services=services,
        )
    specs = _initialize_stage_manifest(task_manager, task, opts, skip_stages, "epic")
    cascade_specs = (
        resolve_stage_manifest_specs(
            task_manager,
            task,
            "epic",
            replace(opts, no_merge=False),
            skip_stages,
        )
        if opts.no_merge
        else specs
    )
    task_manager.cascade_build_state_to_subtree(
        task.id,
        isolation=opts.isolation,
        unattended=False,
        skip_stages=skip_stages,
        allow_automation=True,
        parent_manifest_specs=cascade_specs,
        include_merge_stage=opts.isolation in {"worktree", "clone"} and not opts.no_merge,
    )
    if opts.isolation == "none":
        _cascade_target_branch_to_subtree(task_manager, task.id, target_branch)
    initial_lifecycle = _current_stage_name(task_manager, task.id, specs)
    _record_build_event(task_manager, task.id, initial_lifecycle)
    tick = await _kick_dispatcher_tick(
        db,
        project_id,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_active_agents=opts.max_active_agents,
    )
    if opts.quick:
        _set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
    return BuildResult(
        task_id=task.id,
        created=False,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=skip_stages,
        tick_dispatched=tick.ticks,
        dispatcher_tick=tick,
        manifest=_specs_payload(specs),
    )


def build_stop(
    *,
    db: DatabaseProtocol,
    project_id: str,
) -> BuildControlResult:
    """Stop future dispatcher ticks for the project build queue."""
    return _set_dispatcher_enabled(db=db, project_id=project_id, enabled=False)


def build_resume(
    *,
    db: DatabaseProtocol,
    project_id: str,
) -> BuildControlResult:
    """Resume dispatcher ticks for the project build queue."""
    return _set_dispatcher_enabled(db=db, project_id=project_id, enabled=True)


def _set_dispatcher_enabled(
    *,
    db: DatabaseProtocol,
    project_id: str,
    enabled: bool,
) -> BuildControlResult:
    job = install_dispatcher_cron_row(db, project_id=project_id)
    next_run = compute_next_run(replace(job, enabled=True)) if enabled else None
    storage = CronJobStorage(db)
    updated = storage.update_job(job.id, enabled=enabled)
    if updated is None:
        raise RuntimeError(f"Dispatcher cron row disappeared during build control: {job.id}")
    updated = storage.update_system_job_bookkeeping(
        job.id,
        next_run_at=next_run.isoformat() if next_run else None,
    )
    if updated is None:
        raise RuntimeError(f"Dispatcher cron row disappeared during build control: {job.id}")

    event_name = "build_resume" if enabled else "build_stop"
    reason = "gobby build resume" if enabled else "gobby build stop"
    event = _record_project_build_event(
        db,
        project_id=project_id,
        event=event_name,
        reason=reason,
        by_actor="build",
    )
    return BuildControlResult(
        project_id=project_id,
        enabled=updated.enabled,
        cron_job_id=updated.id,
        lifecycle_event=event,
    )


def _resolve_input(
    input_ref: str,
    task_manager: LocalTaskManager,
    project_id: str,
) -> tuple[InputKind, Task | Path]:
    if _looks_like_task_ref(input_ref):
        task = task_manager.get_task(input_ref, project_id=project_id)
        return ("epic" if task.task_type == "epic" else "leaf", task)

    plan_file = Path(input_ref)
    if not plan_file.exists() or not plan_file.is_file():
        raise ValueError(f"plan file not found: {input_ref}")
    return "plan_file", plan_file


def _validate_skip_stages(skip_stages: list[str]) -> list[str]:
    normalized: list[str] = []
    for stage in skip_stages:
        canonical = _canonical_stage_name_or_none(stage)
        if canonical is None:
            continue
        if canonical not in _CANONICAL_STAGE_NAMES:
            allowed = ", ".join(sorted(_CANONICAL_STAGE_NAMES | set(_SKIPPABLE_STAGE_ORDER)))
            raise ValueError(f"invalid skip stage {stage}; valid skip stages: {allowed}")
        normalized.append(canonical)
    return list(dict.fromkeys(normalized))


def _validate_no_merge(opts: BuildOptions) -> None:
    if opts.no_merge and opts.isolation == "none":
        raise ValueError("--no-merge requires worktree or clone build workspace backend")


def _validate_clones_dir(opts: BuildOptions) -> None:
    if opts.isolation != "clone" or opts.clones_dir is None:
        return
    if not os.access(opts.clones_dir, os.W_OK):
        raise ValueError(f"clones_dir must be writable for clone isolation: {opts.clones_dir}")


def _validate_retry_caps(opts: BuildOptions) -> None:
    if opts.max_retries is not None and opts.max_retries < 0:
        raise ValueError("max_retries must be greater than or equal to 0")
    for override in opts.stage_caps:
        if override.max_work_attempts is not None and override.max_work_attempts < 1:
            raise ValueError(
                f"stage_caps.{override.stage_name}.max_work_attempts must be greater than or equal to 1"
            )
        if override.max_review_rounds is not None and override.max_review_rounds < 1:
            raise ValueError(
                f"stage_caps.{override.stage_name}.max_review_rounds must be greater than or equal to 1"
            )


def _validate_max_active_agents(opts: BuildOptions) -> None:
    if opts.max_active_agents is not None and opts.max_active_agents < 1:
        raise ValueError("max_active_agents must be greater than or equal to 1")


async def _resume_existing_lifecycle(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    db: DatabaseProtocol,
    project_id: str,
    services: object | None,
    target_branch: str | None,
) -> BuildResult:
    if skip_stages:
        raise ValueError(
            "--skip-stage can only shape a new lifecycle; use build restart or clean first"
        )
    _apply_stage_caps_to_existing_lifecycle(task_manager, task.id, opts)
    _validate_task_ref_isolation_artifacts(task_manager, task, opts.isolation)
    task_manager.update_task(
        task.id,
        allow_automation=True,
        unattended=False,
        isolation=opts.isolation,
        assigned_agent=(
            opts.assigned_agent if opts.assigned_agent is not None else task.assigned_agent
        ),
    )
    if task.task_type == "epic":
        artifacts = task_manager.artifacts.get_artifacts(task.id)
        integration_target = target_branch or artifacts.target_branch
        if opts.isolation in {"worktree", "clone"}:
            if integration_target is None:
                raise ValueError("target_branch is required for epic integration workspaces")
            ensure_epic_integration_workspaces(
                task_manager=task_manager,
                root_task=task,
                backend=opts.workspace_backend,
                target_branch=integration_target,
                project_id=project_id,
                services=services,
            )
        task_manager.cascade_build_state_to_subtree(
            task.id,
            isolation=opts.isolation,
            unattended=False,
            allow_automation=True,
            include_merge_stage=opts.isolation in {"worktree", "clone"} and not opts.no_merge,
        )
    specs = _stage_state_specs(task_manager, task.id)
    initial_lifecycle = _current_stage_name(task_manager, task.id, specs)
    _record_build_event(task_manager, task.id, initial_lifecycle)
    tick = await _kick_dispatcher_tick(
        db,
        project_id,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_active_agents=opts.max_active_agents,
    )
    if opts.quick:
        _set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
    return BuildResult(
        task_id=task.id,
        created=False,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=[],
        tick_dispatched=tick.ticks,
        dispatcher_tick=tick,
        manifest=_specs_payload(specs),
    )


def _apply_stage_caps_to_existing_lifecycle(
    task_manager: LocalTaskManager,
    task_id: str,
    opts: BuildOptions,
) -> None:
    if not opts.stage_caps and opts.max_retries is None:
        return
    rows = task_manager.stage_states.list_for_task(task_id)
    stage_names = {row.stage_name for row in rows}
    overrides = {_canonical_stage_name(item.stage_name): item for item in opts.stage_caps}
    retry_cap = retry_attempt_cap(opts)
    for override in opts.stage_caps:
        stage_name = _canonical_stage_name(override.stage_name)
        if stage_name not in stage_names:
            raise ValueError(f"--stage target stage is not in the existing lifecycle: {stage_name}")
    for stage_name in stage_names:
        stage_override = overrides.get(stage_name)
        updates: list[str] = []
        params: list[int | str] = []
        max_work_attempts = (
            stage_override.max_work_attempts
            if stage_override and stage_override.max_work_attempts is not None
            else retry_cap
        )
        max_review_rounds = (
            stage_override.max_review_rounds
            if stage_override and stage_override.max_review_rounds is not None
            else retry_cap
        )
        if max_work_attempts is not None:
            updates.append("max_work_attempts = ?")
            params.append(max_work_attempts)
        if max_review_rounds is not None:
            updates.append("max_review_rounds = ?")
            params.append(max_review_rounds)
        if not updates:
            continue
        params.extend([task_id, stage_name])
        task_manager.db.execute(
            f"""
            UPDATE task_stage_states
               SET {", ".join(updates)}
             WHERE task_id = ? AND stage_name = ?
            """,
            tuple(params),
        )


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
    repo_path = Path(project.repo_path) if project is not None and project.repo_path else Path.cwd()
    if not (repo_path / ".git").exists():
        repo_path = Path.cwd()

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


def _validate_epic_isolation_artifacts(isolation: Isolation, artifacts: TaskArtifacts) -> None:
    if isolation == "clone" and artifacts.worktree_path:
        raise ValueError(f"task already has worktree artifact: {artifacts.worktree_path}")
    if isolation == "worktree" and artifacts.clone_path:
        raise ValueError(f"task already has clone artifact: {artifacts.clone_path}")


def _validate_task_ref_isolation_artifacts(
    task_manager: LocalTaskManager,
    task: Task,
    isolation: Isolation,
) -> None:
    artifacts = task_manager.artifacts.get_artifacts(task.id)
    _validate_epic_isolation_artifacts(isolation, artifacts)


def _quick_tick_limit(opts: BuildOptions) -> int | None:
    return 2 if opts.quick else None


def _set_automation_for_task_tree(
    task_manager: LocalTaskManager,
    task: Task,
    enabled: bool,
    *,
    isolation: Isolation,
) -> None:
    if task.task_type != "epic":
        task_manager.update_task(task.id, allow_automation=enabled)
        return
    task_manager.cascade_build_state_to_subtree(
        task.id,
        isolation=isolation,
        unattended=False,
        allow_automation=enabled,
    )


def _prepare_task_ref_expansion_output(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
) -> None:
    from gobby.tasks.expansion_service import ExpansionService

    service = ExpansionService(task_manager=task_manager, llm_service=None)
    if opts.reset_expansion_output:
        service.reset_expansion_output(task.id)
        return
    existing = service.find_existing_expansion_output(task.id)
    if existing is not None:
        raise ValueError(
            "Expansion output already exists for this task. "
            "Use --reset-expansion-output before rebuilding."
        )


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


def _current_stage_name(
    task_manager: LocalTaskManager,
    task_id: str,
    specs: list[StageManifestSpec],
) -> str:
    current = task_manager.stage_states.current_stage(task_id)
    if current is not None:
        return current.stage_name
    return min(specs, key=lambda spec: spec.position).stage_name


def _record_build_event(
    task_manager: LocalTaskManager,
    task_id: str,
    to_state: str,
) -> None:
    task_manager.lifecycle_events.record_lifecycle_event(
        task_id,
        from_state=None,
        to_state=to_state,
        reason="gobby build",
        by_actor="build",
    )


def _initialize_stage_manifest(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    input_kind: InputKind,
) -> list[StageManifestSpec]:
    specs = resolve_stage_manifest_specs(task_manager, task, input_kind, opts, skip_stages)
    try:
        task_manager.stage_states.initialize_manifest(task.id, specs, by_session_id=None)
    except ManifestAlreadyInitializedError as exc:
        raise ValueError(
            "Task already has a different lifecycle manifest. "
            f"Use `gobby build restart {task.id}` or `gobby build clean {task.id}` "
            "before changing the build stage shape."
        ) from exc
    return specs


def resolve_stage_manifest_specs(
    task_manager: LocalTaskManager,
    task: Task,
    input_kind: InputKind,
    opts: BuildOptions,
    skip_stages: list[str] | None = None,
) -> list[StageManifestSpec]:
    """Resolve explicit/default build stage flags to StageManifestSpec rows."""

    manifest = _initial_stage_names(task_manager, task, input_kind, opts)

    skipped = {
        canonical
        for stage in (skip_stages or [])
        if (canonical := _canonical_stage_name_or_none(stage)) is not None
    }
    manifest = [stage_name for stage_name in manifest if stage_name not in skipped]

    cap_by_stage = {
        _canonical_stage_name(override.stage_name): override for override in opts.stage_caps
    }
    retry_cap = retry_attempt_cap(opts)
    unknown_caps = sorted(set(cap_by_stage) - set(manifest))
    if unknown_caps:
        raise ValueError(f"--stage target stage not in resolved manifest: {unknown_caps[0]}")

    specs: list[StageManifestSpec] = []
    for position, stage_name in enumerate(manifest):
        override = cap_by_stage.get(stage_name)
        specs.append(
            StageManifestSpec(
                stage_name=stage_name,
                position=position,
                max_work_attempts=(
                    override.max_work_attempts
                    if override and override.max_work_attempts is not None
                    else retry_cap
                ),
                max_review_rounds=(
                    override.max_review_rounds
                    if override and override.max_review_rounds is not None
                    else retry_cap
                ),
            )
        )
    return specs


def _initial_stage_names(
    task_manager: LocalTaskManager,
    task: Task,
    input_kind: InputKind,
    opts: BuildOptions,
) -> list[str]:
    if opts.stage_caps:
        manifest = [_canonical_stage_name(override.stage_name) for override in opts.stage_caps]
    elif input_kind == "leaf":
        manifest = [_leaf_primary_stage(task)]
    elif input_kind == "plan_file" and opts.quick:
        manifest = ["planning"]
    elif input_kind == "plan_file":
        manifest = ["planning", "expansion", "development", "holistic_qa", "pr", "merge"]
    else:
        defaults = task_manager.stages_registry.list_default_stages(task.task_type)
        if not defaults and task.task_type != "task":
            defaults = task_manager.stages_registry.list_default_stages("task")
        manifest = [_canonical_stage_name(stage_name) for stage_name, _position in defaults]

    manifest = list(dict.fromkeys(manifest))
    if opts.pr and "pr" not in manifest:
        _insert_before_merge(manifest, "pr")
    if opts.isolation in {"worktree", "clone"} and not opts.no_merge and "merge" not in manifest:
        manifest.append("merge")
    if opts.no_merge:
        manifest = [stage_name for stage_name in manifest if stage_name != "merge"]
    if opts.isolation == "none" and not opts.pr and input_kind == "leaf":
        manifest = [stage_name for stage_name in manifest if stage_name not in {"pr", "merge"}]
    return manifest


def _leaf_primary_stage(task: Task) -> str:
    category = task.category or ""
    stage_name = LEAF_PRIMARY_STAGE_BY_CATEGORY.get(category)
    if stage_name is None:
        if category == "manual":
            raise ValueError("manual leaf tasks are not automatable")
        allowed = ", ".join(sorted(AUTOMATED_LEAF_CATEGORIES))
        raise ValueError(f"category {category} cannot be automated; expected one of: {allowed}")
    return stage_name


def _insert_before_merge(manifest: list[str], stage_name: str) -> None:
    if "merge" in manifest:
        manifest.insert(manifest.index("merge"), stage_name)
        return
    manifest.append(stage_name)


def _stage_state_specs(
    task_manager: LocalTaskManager,
    task_id: str,
) -> list[StageManifestSpec]:
    return [
        StageManifestSpec(
            stage_name=row.stage_name,
            position=row.position,
            max_work_attempts=row.max_work_attempts,
            max_review_rounds=row.max_review_rounds,
        )
        for row in task_manager.stage_states.list_for_task(task_id)
    ]


def _canonical_stage_name(stage_name: str) -> str:
    canonical = _canonical_stage_name_or_none(stage_name)
    if canonical is None:
        raise ValueError(f"stage {stage_name} no longer exists in the stage manifest")
    if canonical not in _CANONICAL_STAGE_NAMES:
        raise ValueError(f"unknown stage: {stage_name}")
    return canonical


def _canonical_stage_name_or_none(stage_name: str) -> str | None:
    normalized = stage_name.strip()
    if not normalized:
        raise ValueError("stage name is required")
    return _LEGACY_STAGE_ALIASES.get(normalized, normalized)


def _specs_payload(specs: list[StageManifestSpec]) -> list[dict[str, str | int | None]]:
    return [asdict(spec) for spec in specs]


def _record_project_build_event(
    db: DatabaseProtocol,
    *,
    project_id: str,
    event: str,
    reason: str,
    by_actor: str,
) -> BuildLifecycleEvent:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS project_lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            event TEXT NOT NULL,
            reason TEXT NOT NULL,
            by_actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_project_lifecycle_events_project
            ON project_lifecycle_events (project_id, created_at)
        """
    )
    created_at = datetime.now(UTC).isoformat()
    cursor = db.execute(
        """
        INSERT INTO project_lifecycle_events (project_id, event, reason, by_actor, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (project_id, event, reason, by_actor, created_at),
    )
    event_id = cursor.lastrowid
    if event_id is None:
        raise RuntimeError("SQLite did not return a project lifecycle event id")
    return BuildLifecycleEvent(
        id=event_id,
        project_id=project_id,
        event=event,
        reason=reason,
        by_actor=by_actor,
        created_at=created_at,
    )


def _looks_like_task_ref(input_ref: str) -> bool:
    return input_ref.startswith("#") or input_ref.isdigit()


__all__ = [
    "AUTOMATED_LEAF_CATEGORIES",
    "BuildControlResult",
    "DispatcherTickSummary",
    "BuildLifecycleEvent",
    "BuildOptions",
    "BuildResult",
    "build",
    "build_resume",
    "build_stop",
    "resolve_stage_manifest_specs",
]
