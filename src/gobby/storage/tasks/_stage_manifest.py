"""Stage manifest helpers for explicit lifecycle initialization.

Encapsulates the parsing of caller-supplied ``stage_caps`` overrides and
the stage-manifest initialization performed by lifecycle entry points.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from gobby.storage.tasks._stage_types import StageManifestSpec

if TYPE_CHECKING:
    from gobby.storage.tasks._stage_registry import StageRegistryManager
    from gobby.storage.tasks._stage_states import StageStatesManager


def _stage_cap_value(stage_name: str, field_name: str, value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"stage_caps.{stage_name}.{field_name} must be an integer >= 1")
    return value


def _stage_cap_overrides(
    stage_caps: Sequence[Mapping[str, object]] | None,
) -> dict[str, tuple[int | None, int | None]]:
    overrides: dict[str, tuple[int | None, int | None]] = {}
    for item in stage_caps or ():
        raw_stage_name = item.get("stage_name")
        if not isinstance(raw_stage_name, str) or not raw_stage_name.strip():
            raise ValueError("stage_caps entries require a non-empty stage_name")
        stage_name = raw_stage_name.strip()
        overrides[stage_name] = (
            _stage_cap_value(stage_name, "max_work_attempts", item.get("max_work_attempts")),
            _stage_cap_value(stage_name, "max_review_rounds", item.get("max_review_rounds")),
        )
    return overrides


def _stage_manifest_specs(
    default_stages: Iterable[tuple[str, int]],
    stage_caps: Sequence[Mapping[str, object]] | None,
) -> list[StageManifestSpec]:
    cap_by_stage = _stage_cap_overrides(stage_caps)
    specs: list[StageManifestSpec] = []
    seen_names: set[str] = set()
    for stage_name, position in default_stages:
        seen_names.add(stage_name)
        work_cap, review_cap = cap_by_stage.get(stage_name, (None, None))
        specs.append(
            StageManifestSpec(
                stage_name=stage_name,
                position=position,
                max_work_attempts=work_cap,
                max_review_rounds=review_cap,
            )
        )

    unknown_caps = sorted(set(cap_by_stage) - seen_names)
    if unknown_caps:
        raise ValueError(f"stage_caps target stage not in task manifest: {unknown_caps[0]}")
    return specs


def resolve_task_manifest_specs(
    stages_registry: StageRegistryManager,
    *,
    task_type: str,
    stage_names: Sequence[str] | None,
    stage_caps: Sequence[Mapping[str, object]] | None,
) -> list[StageManifestSpec]:
    """Resolve task-type defaults or explicit stage names into manifest specs.

    When ``stage_names`` is supplied, every named stage must exist in the
    registry. Otherwise the registry's default stages for the task type are
    used. ``stage_caps`` may override per-stage attempt limits.
    """
    if stage_names is not None:
        for stage_name in stage_names:
            if stages_registry.get(stage_name) is None:
                raise ValueError(f"Unknown stage '{stage_name}'")
        default_stages: list[tuple[str, int]] = [
            (stage_name, position) for position, stage_name in enumerate(stage_names)
        ]
    else:
        default_stages = stages_registry.list_default_stages(task_type)
        if not default_stages and task_type != "task":
            default_stages = stages_registry.list_default_stages("task")
    return _stage_manifest_specs(default_stages, stage_caps)


def initialize_task_manifest_for_task(
    stages_registry: StageRegistryManager,
    stage_states: StageStatesManager,
    task_id: str,
    *,
    task_type: str,
    stage_names: Sequence[str] | None,
    stage_caps: Sequence[Mapping[str, object]] | None,
    by_session_id: str | None,
) -> list[Any]:
    """Resolve and persist a task manifest through an explicit lifecycle entry point."""
    specs = resolve_task_manifest_specs(
        stages_registry,
        task_type=task_type,
        stage_names=stage_names,
        stage_caps=stage_caps,
    )
    if not specs:
        return []
    return stage_states.initialize_manifest(task_id, specs, by_session_id=by_session_id)


def derive_child_manifest_specs(
    parent_rows: Sequence[Any],
    *,
    include_holistic_qa: bool,
    include_merge_stage: bool = False,
) -> list[StageManifestSpec]:
    """Derive generated child stage rows from a resolved parent manifest."""
    by_name = {
        str(row.stage_name): row for row in sorted(parent_rows, key=lambda item: item.position)
    }
    stage_names: list[str] = []
    if "development" in by_name:
        stage_names.append("development")
    if include_holistic_qa and "holistic_qa" in by_name:
        stage_names.append("holistic_qa")
    if "pr" in by_name:
        stage_names.append("pr")
    if "merge" in by_name or include_merge_stage:
        stage_names.append("merge")

    specs: list[StageManifestSpec] = []
    for position, stage_name in enumerate(stage_names):
        source = by_name.get(stage_name)
        specs.append(
            StageManifestSpec(
                stage_name=stage_name,
                position=position,
                max_work_attempts=getattr(source, "max_work_attempts", None),
                max_review_rounds=getattr(source, "max_review_rounds", None),
            )
        )
    return specs
