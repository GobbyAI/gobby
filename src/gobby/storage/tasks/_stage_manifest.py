"""Stage manifest helpers for task creation.

Encapsulates the parsing of caller-supplied ``stage_caps`` overrides and
the stage-manifest initialization performed when a task is created.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

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
    stages_override: Sequence[str] | None = None,
) -> list[StageManifestSpec]:
    cap_by_stage = _stage_cap_overrides(stage_caps)
    if stages_override is not None:
        default_stages = [
            (stage_name, position) for position, stage_name in enumerate(stages_override)
        ]
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


def init_stage_manifest_for_task(
    stages_registry: StageRegistryManager,
    stage_states: StageStatesManager,
    task_id: str,
    *,
    task_type: str,
    stage_caps: Sequence[Mapping[str, object]] | None,
    stages_override: Sequence[str] | None,
    by_session_id: str | None,
) -> None:
    """Resolve the stage manifest for a newly created task and persist it.

    When ``stages_override`` is supplied, every named stage must exist in
    the registry. Otherwise the registry's default stages for the task
    type are used. ``stage_caps`` may override per-stage attempt limits.
    """
    if stages_override is not None:
        for stage_name in stages_override:
            if stages_registry.get(stage_name) is None:
                raise ValueError(f"Unknown stage '{stage_name}'")
        default_stages: list[tuple[str, int]] = [
            (stage_name, position) for position, stage_name in enumerate(stages_override)
        ]
    else:
        default_stages = stages_registry.list_default_stages(task_type)
    specs = _stage_manifest_specs(default_stages, stage_caps)
    if specs:
        stage_states.initialize_manifest(task_id, specs, by_session_id=by_session_id)
