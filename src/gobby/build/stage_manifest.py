"""Build stage manifest resolution."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from gobby.build.options import BuildOptions, retry_attempt_cap
from gobby.storage.tasks import LocalTaskManager, StageManifestSpec, Task

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


def stage_state_specs(
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


def specs_payload(specs: list[StageManifestSpec]) -> list[dict[str, str | int | None]]:
    return [asdict(spec) for spec in specs]


__all__ = [
    "AUTOMATED_LEAF_CATEGORIES",
    "InputKind",
    "resolve_stage_manifest_specs",
    "specs_payload",
    "stage_state_specs",
]
