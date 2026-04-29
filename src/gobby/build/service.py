"""Shared build service for lifecycle automation entry points."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from gobby.config.build import SKIPPABLE_STAGES, Isolation
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskArtifacts


@dataclass
class BuildOptions:
    """Resolved options for a build request."""

    profile: str | None
    skip_stages: list[str]
    isolation: Isolation
    yolo: bool
    max_review_rounds: int
    max_expansion_attempts: int | None = None
    max_qa_rounds: int | None = None
    max_merge_attempts: int | None = None
    max_holistic_rounds: int | None = None
    target_branch: str | None = None
    assigned_agent: str | None = None
    clones_dir: Path | None = None


class RetryCaps(TypedDict):
    max_expansion_attempts: int | None
    max_qa_rounds: int | None
    max_merge_attempts: int | None
    max_holistic_rounds: int | None
    max_review_rounds: int


@dataclass
class BuildResult:
    """Summary returned by build service surfaces."""

    task_id: str
    created: bool
    initial_lifecycle: str
    applied_stages_skipped: list[str]
    tick_dispatched: int
    retry_caps: RetryCaps | None = None


AUTOMATED_LEAF_CATEGORIES = frozenset({"code", "config", "docs", "test"})
InputKind = Literal["plan_file", "epic", "leaf"]

_SKIPPABLE_STAGE_ORDER = (
    "plan_review",
    "test_arch",
    "expanding",
    "qa",
    "holistic_review",
    "pr",
)
_PLAN_START_SEQUENCE = ("plan_review", "test_arch", "expanding", "in_development")


async def build(
    input_ref: str,
    opts: BuildOptions,
    *,
    db: DatabaseProtocol,
    project_id: str,
) -> BuildResult:
    """Start lifecycle automation for a plan file, epic, or automated leaf task."""

    skip_stages = _validate_skip_stages(opts.skip_stages)
    task_manager = LocalTaskManager(db)
    input_kind, task_or_plan = _resolve_input(input_ref, task_manager, project_id)

    _validate_profile_for_input(opts.profile, input_kind)
    _validate_clones_dir(opts)
    _validate_retry_caps(opts)
    await _validate_target_branch(db, project_id, opts.target_branch)

    if input_kind == "plan_file":
        assert isinstance(task_or_plan, Path)
        return _build_plan_file(task_manager, task_or_plan, opts, skip_stages, project_id)

    assert isinstance(task_or_plan, Task)
    task = task_or_plan
    if input_kind == "leaf":
        return _build_leaf(task_manager, task, opts, skip_stages)

    return _build_epic(task_manager, task, opts, skip_stages)


def _build_plan_file(
    task_manager: LocalTaskManager,
    plan_file: Path,
    opts: BuildOptions,
    skip_stages: list[str],
    project_id: str,
) -> BuildResult:
    initial_lifecycle = _initial_lifecycle_for_plan(skip_stages)
    task = task_manager.create_task(
        project_id=project_id,
        title=f"Build {plan_file.name}",
        description=f"Lifecycle automation seeded from plan file: {plan_file}",
        task_type="epic",
        category="planning",
    )
    labels = _stage_labels(skip_stages)
    task_manager.update_task(
        task.id,
        labels=labels,
        lifecycle=initial_lifecycle,
        allow_automation=True,
        yolo=opts.yolo,
        isolation=opts.isolation,
        assigned_agent=opts.assigned_agent,
    )
    task_manager.artifacts.set_artifacts_atomic(
        task.id,
        plan_file_path=str(plan_file),
        target_branch=opts.target_branch,
        **_retry_cap_artifacts(opts),
    )
    _record_build_event(task_manager, task, initial_lifecycle)
    return BuildResult(
        task_id=task.id,
        created=True,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=skip_stages,
        tick_dispatched=_kick_dispatcher_tick(),
        retry_caps=_retry_cap_artifacts(opts),
    )


def _build_leaf(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
) -> BuildResult:
    if opts.isolation != "none":
        raise ValueError("isolation requires an epic; leaf builds must use isolation none")
    if task.category not in AUTOMATED_LEAF_CATEGORIES:
        allowed = ", ".join(sorted(AUTOMATED_LEAF_CATEGORIES))
        raise ValueError(
            f"category {task.category} cannot be automated; expected one of: {allowed}"
        )

    initial_lifecycle = "in_development"
    task_manager.update_task(
        task.id,
        labels=_merge_stage_labels(task.labels, skip_stages),
        lifecycle=initial_lifecycle,
        allow_automation=True,
        yolo=opts.yolo,
        isolation="none",
        assigned_agent=opts.assigned_agent,
    )
    task_manager.artifacts.set_artifacts_atomic(task.id, **_retry_cap_artifacts(opts))
    _record_build_event(task_manager, task, initial_lifecycle)
    return BuildResult(
        task_id=task.id,
        created=False,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=skip_stages,
        tick_dispatched=_kick_dispatcher_tick(),
        retry_caps=_retry_cap_artifacts(opts),
    )


def _build_epic(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
) -> BuildResult:
    artifacts = task_manager.artifacts.get_artifacts(task.id)
    _validate_epic_isolation_artifacts(opts.isolation, artifacts)
    task_manager.artifacts.set_artifacts_atomic(
        task.id,
        target_branch=opts.target_branch,
        **_retry_cap_artifacts(opts),
    )
    task_manager.cascade_build_state_to_subtree(
        task.id,
        isolation=opts.isolation,
        yolo=opts.yolo,
        skip_stage_labels=_stage_labels(skip_stages),
        allow_automation=True,
    )
    initial_lifecycle = str(task.lifecycle)
    _record_build_event(task_manager, task, initial_lifecycle)
    return BuildResult(
        task_id=task.id,
        created=False,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=skip_stages,
        tick_dispatched=_kick_dispatcher_tick(),
        retry_caps=_retry_cap_artifacts(opts),
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
    unknown = [stage for stage in skip_stages if stage not in SKIPPABLE_STAGES]
    if unknown:
        allowed = ", ".join(_SKIPPABLE_STAGE_ORDER)
        raise ValueError(f"invalid skip stage {unknown[0]}; valid skip stages: {allowed}")
    return list(dict.fromkeys(skip_stages))


def _validate_profile_for_input(profile: str | None, input_kind: InputKind) -> None:
    if profile == "quick" and input_kind == "plan_file":
        raise ValueError(
            "quick profile is only valid for leaf tasks; plan files need review or full"
        )


def _validate_clones_dir(opts: BuildOptions) -> None:
    if opts.isolation != "clone" or opts.clones_dir is None:
        return
    if not os.access(opts.clones_dir, os.W_OK):
        raise ValueError(f"clones_dir must be writable for clone isolation: {opts.clones_dir}")


def _validate_retry_caps(opts: BuildOptions) -> None:
    for field in (
        "max_review_rounds",
        "max_expansion_attempts",
        "max_qa_rounds",
        "max_merge_attempts",
        "max_holistic_rounds",
    ):
        value = getattr(opts, field)
        if value is not None and value < 1:
            raise ValueError(f"{field} must be greater than or equal to 1")


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
        "branch",
        "--list",
        target_branch,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        stderr = stderr_bytes.decode().strip()
        detail = f": {stderr}" if stderr else ""
        raise ValueError(f"failed to inspect git branches for {repo_path}{detail}")
    if stdout_bytes.decode().strip():
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


def _validate_epic_isolation_artifacts(isolation: Isolation, artifacts: TaskArtifacts) -> None:
    if isolation == "clone" and artifacts.worktree_path:
        raise ValueError(f"task already has worktree artifact: {artifacts.worktree_path}")
    if isolation == "worktree" and artifacts.clone_path:
        raise ValueError(f"task already has clone artifact: {artifacts.clone_path}")


def _initial_lifecycle_for_plan(skip_stages: list[str]) -> str:
    skipped = set(skip_stages)
    for stage in _PLAN_START_SEQUENCE:
        if stage not in skipped:
            return stage
    return "in_development"


def _stage_labels(skip_stages: list[str]) -> list[str]:
    return [f"stage-:{stage}" for stage in skip_stages]


def _merge_stage_labels(existing: list[str] | None, skip_stages: list[str]) -> list[str]:
    labels = list(existing or [])
    seen = set(labels)
    for label in _stage_labels(skip_stages):
        if label not in seen:
            labels.append(label)
            seen.add(label)
    return labels


def _record_build_event(
    task_manager: LocalTaskManager,
    task: Task,
    to_state: str,
) -> None:
    task_manager.lifecycle_events.record_lifecycle_event(
        task.id,
        from_state=str(task.lifecycle),
        to_state=to_state,
        reason="gobby build",
        by_actor="build",
    )


def _retry_cap_artifacts(opts: BuildOptions) -> RetryCaps:
    return {
        "max_expansion_attempts": opts.max_expansion_attempts,
        "max_qa_rounds": opts.max_qa_rounds,
        "max_merge_attempts": opts.max_merge_attempts,
        "max_holistic_rounds": opts.max_holistic_rounds,
        "max_review_rounds": opts.max_review_rounds,
    }


def _kick_dispatcher_tick() -> int:
    """Placeholder for dispatcher-tick wiring; tracked as a separate follow-up task."""
    return 0


def _looks_like_task_ref(input_ref: str) -> bool:
    return input_ref.startswith("#") or input_ref.isdigit()


__all__ = ["AUTOMATED_LEAF_CATEGORIES", "BuildOptions", "BuildResult", "RetryCaps", "build"]
