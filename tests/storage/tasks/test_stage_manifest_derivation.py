"""Unit coverage for child stage-manifest derivation."""

from __future__ import annotations

import pytest

from gobby.storage.tasks._stage_manifest import derive_child_manifest_specs
from gobby.storage.tasks._stage_types import StageManifestSpec


def _parent(*stage_names: str) -> list[StageManifestSpec]:
    return [
        StageManifestSpec(stage_name=stage_name, position=position)
        for position, stage_name in enumerate(stage_names)
    ]


@pytest.mark.unit
@pytest.mark.parametrize("include_merge_stage", [False, True])
def test_expansion_only_parent_derives_no_child_stages(include_merge_stage: bool) -> None:
    specs = derive_child_manifest_specs(
        _parent("expansion"),
        include_epic_qa=True,
        include_merge_stage=include_merge_stage,
    )

    assert specs == []


@pytest.mark.unit
def test_parent_with_merge_but_no_work_stage_derives_no_child_stages() -> None:
    specs = derive_child_manifest_specs(
        _parent("expansion", "merge"),
        include_epic_qa=True,
        include_merge_stage=False,
    )

    assert specs == []


@pytest.mark.unit
def test_full_lifecycle_parent_appends_merge_as_terminal_stage() -> None:
    specs = derive_child_manifest_specs(
        _parent("development", "epic_qa", "pr"),
        include_epic_qa=True,
        include_merge_stage=True,
    )

    assert [(spec.stage_name, spec.position) for spec in specs] == [
        ("development", 0),
        ("epic_qa", 1),
        ("pr", 2),
        ("merge", 3),
    ]


@pytest.mark.unit
def test_development_only_parent_still_forces_merge_when_requested() -> None:
    specs = derive_child_manifest_specs(
        _parent("development"),
        include_epic_qa=False,
        include_merge_stage=True,
    )

    assert [(spec.stage_name, spec.position) for spec in specs] == [
        ("development", 0),
        ("merge", 1),
    ]


@pytest.mark.unit
def test_parent_merge_row_is_inherited_without_the_force_flag() -> None:
    specs = derive_child_manifest_specs(
        _parent("development", "merge"),
        include_epic_qa=False,
        include_merge_stage=False,
    )

    assert [(spec.stage_name, spec.position) for spec in specs] == [
        ("development", 0),
        ("merge", 1),
    ]


@pytest.mark.unit
def test_epic_qa_is_dropped_when_not_requested() -> None:
    specs = derive_child_manifest_specs(
        _parent("development", "epic_qa", "pr", "merge"),
        include_epic_qa=False,
        include_merge_stage=False,
    )

    assert [spec.stage_name for spec in specs] == ["development", "pr", "merge"]


@pytest.mark.unit
def test_stage_caps_are_carried_from_the_parent_row() -> None:
    parent_rows = [
        StageManifestSpec(
            stage_name="development",
            position=0,
            max_work_attempts=4,
            max_review_rounds=2,
        ),
    ]

    specs = derive_child_manifest_specs(
        parent_rows,
        include_epic_qa=False,
        include_merge_stage=True,
    )

    assert specs[0].max_work_attempts == 4
    assert specs[0].max_review_rounds == 2
    assert specs[1].stage_name == "merge"
    assert specs[1].max_work_attempts is None
