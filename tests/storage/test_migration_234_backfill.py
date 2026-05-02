"""Phase 2 red contracts for migration 234 stage-manifest backfill."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MIGRATIONS,
    _apply_baseline,
    _run_migration_list,
    get_current_version,
)
from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

PERSONAL_PROJECT_ID = "00000000-0000-0000-0000-000000060887"


def _migration_234():
    for migration in MIGRATIONS:
        if migration[0] == 234:
            return migration
    pytest.fail("migration version 234 is missing from MIGRATIONS")


def _db_before_234(tmp_path: Path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "before-234-backfill.db")
    _apply_baseline(db)
    pre_234 = [migration for migration in MIGRATIONS if BASELINE_VERSION < migration[0] < 234]
    _run_migration_list(db, BASELINE_VERSION, pre_234)
    return db


def _apply_234(db: LocalDatabase) -> None:
    current_version = get_current_version(db)
    assert current_version < 234
    _run_migration_list(db, current_version, [_migration_234()])


def _insert_task(
    db: LocalDatabase,
    task_id: str,
    *,
    task_type: str = "feature",
    lifecycle: str = "open",
    status: str = "open",
    labels: list[str] | None = None,
    closed_at: str | None = None,
    escalated_at: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO tasks (
            id, project_id, title, task_type, lifecycle, status, labels,
            closed_at, escalated_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            PERSONAL_PROJECT_ID,
            f"Task {task_id}",
            task_type,
            lifecycle,
            status,
            json.dumps(labels) if labels else None,
            closed_at,
            escalated_at,
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        ),
    )


def _stage_rows(db: LocalDatabase, task_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.fetchall(
            """
            SELECT stage_name, position, state, review_policy, reviewer_agent,
                   work_attempt_count, review_round_count, max_work_attempts,
                   max_review_rounds, artifact_refs
              FROM task_stage_states
             WHERE task_id = ?
             ORDER BY position
            """,
            (task_id,),
        )
    ]


def _state_map(db: LocalDatabase, task_id: str) -> dict[str, str]:
    return {row["stage_name"]: row["state"] for row in _stage_rows(db, task_id)}


register_contract_tests(
    globals(),
    {
        "test_census_exhaustive_over_conductor_stage_anchors": (
            "migration 234 census covers every conductor-stage override anchor"
        ),
        "test_census_exhaustive_over_full_cross_product": (
            "migration 234 census covers every observed lifecycle/status tuple"
        ),
        "test_close_pass_does_not_overwrite_existing_closed_at": (
            "close pass preserves existing closed_at values"
        ),
        "test_close_pass_sets_closed_at_for_all_done_open_tasks": (
            "close pass closes all-done open manifests with migration:234 session"
        ),
        "test_close_pass_skips_tasks_with_no_manifest_rows": (
            "close pass skips tasks without manifest rows"
        ),
        "test_conductor_stage_expansion_anchor_review_approved_via_2_6_review_tool": (
            "migrated expansion anchor can be approved through stage-native review tools"
        ),
        "test_conductor_stage_expansion_closed_to_done": (
            "conductor-stage:expansion closed maps to expansion.done"
        ),
        "test_conductor_stage_expansion_label_dropped": (
            "conductor-stage:expansion label is removed after backfill"
        ),
        "test_conductor_stage_expansion_needs_review": (
            "conductor-stage:expansion needs_review maps to expansion.needs_review"
        ),
        "test_conductor_stage_expansion_open_in_progress": (
            "conductor-stage:expansion open maps to expansion.in_progress"
        ),
        "test_conductor_stage_expansion_review_approved": (
            "conductor-stage:expansion review_approved maps to expansion.review_approved"
        ),
        "test_conductor_stage_planning_anchor_review_approved_via_2_6_review_tool": (
            "migrated planning anchor can be approved through stage-native review tools"
        ),
        "test_conductor_stage_planning_anchor_review_rejected_via_2_6_review_tool": (
            "migrated planning anchor can be rejected through stage-native review tools"
        ),
        "test_conductor_stage_planning_closed_to_done": (
            "conductor-stage:planning closed maps to planning.done"
        ),
        "test_conductor_stage_planning_label_dropped": (
            "conductor-stage:planning label is removed after backfill"
        ),
        "test_conductor_stage_planning_needs_review": (
            "conductor-stage:planning needs_review maps to planning.needs_review"
        ),
        "test_conductor_stage_planning_open_in_progress": (
            "conductor-stage:planning open maps to planning.in_progress"
        ),
        "test_conductor_stage_planning_review_approved": (
            "conductor-stage:planning review_approved maps to planning.review_approved"
        ),
        "test_conductor_stage_requirements_closed_to_done": (
            "conductor-stage:requirements closed maps to prd.done"
        ),
        "test_conductor_stage_requirements_label_dropped": (
            "conductor-stage:requirements label is removed after backfill"
        ),
        "test_conductor_stage_requirements_needs_review_collapses_to_in_progress": (
            "prd policy-none anchor collapses needs_review to in_progress"
        ),
        "test_conductor_stage_requirements_open_in_progress": (
            "conductor-stage:requirements open maps to prd.in_progress"
        ),
        "test_conductor_stage_requirements_review_approved_collapses_to_done": (
            "prd policy-none anchor collapses review_approved to done"
        ),
        "test_conductor_stage_test_architecture_anchor_advances_via_complete_stage": (
            "migrated test_arch anchor advances via complete_stage rather than review tools"
        ),
        "test_conductor_stage_test_architecture_closed_to_done": (
            "conductor-stage:test-architecture closed maps to test_arch.done"
        ),
        "test_conductor_stage_test_architecture_label_dropped": (
            "conductor-stage:test-architecture label is removed after backfill"
        ),
        "test_conductor_stage_test_architecture_needs_review_collapses_to_in_progress": (
            "test_arch policy-none anchor collapses needs_review to in_progress"
        ),
        "test_conductor_stage_test_architecture_open_in_progress": (
            "conductor-stage:test-architecture open maps to test_arch.in_progress"
        ),
        "test_conductor_stage_test_architecture_review_approved_collapses_to_done": (
            "test_arch policy-none anchor collapses review_approved to done"
        ),
        "test_default_manifest_bug": "bug defaults to [development, pr, merge]",
        "test_default_manifest_chore": "chore defaults to [development, pr, merge]",
        "test_default_manifest_epic": (
            "epic defaults to the full 11-stage pipeline including holistic_qa"
        ),
        "test_default_manifest_feature": (
            "feature defaults to [planning, test_arch, expansion, development, pr, merge]"
        ),
        "test_default_manifest_refactor": (
            "refactor defaults to [planning, development, pr, merge]"
        ),
        "test_default_manifest_task": "task defaults to [development, pr, merge]",
        "test_is_escalated_backfilled": (
            "tasks.is_escalated is backfilled from escalated_at and legacy status"
        ),
        "test_map_expanding_needs_review": (
            "expanding needs_review maps to expansion.needs_review"
        ),
        "test_map_expanding_open": "expanding open maps to expansion.in_progress",
        "test_map_expanding_review_approved": (
            "expanding review_approved maps to expansion.review_approved"
        ),
        "test_map_holistic_review_approved": (
            "holistic_review review_approved maps to holistic_qa.review_approved"
        ),
        "test_map_holistic_review_needs_review": (
            "holistic_review needs_review maps to holistic_qa.needs_review"
        ),
        "test_map_holistic_review_open": ("holistic_review open maps to holistic_qa.in_progress"),
        "test_map_in_development_approved": (
            "in_development review_approved maps to development.review_approved"
        ),
        "test_map_in_development_needs_review": (
            "in_development needs_review maps to development.needs_review"
        ),
        "test_map_in_development_open": ("in_development open maps to development.in_progress"),
        "test_map_merged_closed": "merged closed maps every manifest row to done",
        "test_map_merging": "merging non-terminal maps merge to in_progress",
        "test_map_open_open": "open/open maps every manifest row to ready",
        "test_map_open_other": "open/other maps every manifest row to ready",
        "test_map_plan_review_approved": (
            "plan_review review_approved maps planning.review_approved"
        ),
        "test_map_plan_review_needs_review": (
            "plan_review needs_review maps planning.needs_review"
        ),
        "test_map_plan_review_open": "plan_review open maps planning.in_progress",
        "test_map_pr_needs_review": "pr needs_review maps pr.needs_review with PR URL",
        "test_map_pr_open": "pr open maps pr.in_progress and merge.ready",
        "test_map_pr_review_approved": "pr review_approved maps pr.review_approved",
        "test_map_test_arch_needs_review_collapses_to_in_progress": (
            "test_arch policy-none needs_review collapses to in_progress"
        ),
        "test_map_test_arch_open": "test_arch open maps test_arch.in_progress",
        "test_map_test_arch_review_approved_collapses_to_done": (
            "test_arch policy-none review_approved collapses to done"
        ),
        "test_mapping_exhaustive": (
            "every observed lifecycle/status tuple produces mapped stage rows"
        ),
        "test_max_expansion_attempts_migrates_to_expansion_work_cap": (
            "max_expansion_attempts migrates to expansion.max_work_attempts"
        ),
        "test_max_holistic_rounds_migrates_to_holistic_qa_review_cap": (
            "max_holistic_rounds migrates to holistic_qa.max_review_rounds"
        ),
        "test_max_merge_attempts_migrates_to_merge_work_cap": (
            "max_merge_attempts migrates to merge.max_work_attempts"
        ),
        "test_max_qa_rounds_migrates_to_development_review_cap": (
            "max_qa_rounds migrates to development.max_review_rounds"
        ),
        "test_max_review_rounds_migrates_to_pr_review_cap": (
            "max_review_rounds migrates to pr.max_review_rounds"
        ),
        "test_no_stranded_open_exhausted_tasks_post_migration": (
            "post migration no task has exhausted manifest and closed_at null"
        ),
        "test_normalize_n1_census_contains_no_escalated_tuples": (
            "pre-normalization removes escalated tuples from the census"
        ),
        "test_normalize_n1_conductor_stage_planning_escalated": (
            "escalated conductor-stage:planning rows normalize before mapping"
        ),
        "test_normalize_n1_expanding_escalated": (
            "escalated expanding rows normalize before mapping"
        ),
        "test_normalize_n1_holistic_review_escalated": (
            "escalated holistic_review rows normalize before mapping"
        ),
        "test_normalize_n1_in_development_escalated": (
            "escalated in_development rows normalize before mapping"
        ),
        "test_normalize_n1_merging_escalated": ("escalated merging rows normalize before mapping"),
        "test_normalize_n1_open_escalated": ("escalated open rows normalize before mapping"),
        "test_normalize_n1_plan_review_escalated": (
            "escalated plan_review rows normalize before mapping"
        ),
        "test_normalize_n1_pr_escalated": "escalated pr rows normalize before mapping",
        "test_normalize_n1_sets_is_escalated_one": ("N1 normalization sets tasks.is_escalated=1"),
        "test_normalize_n1_test_arch_escalated": (
            "escalated test_arch rows normalize before mapping"
        ),
        "test_normalize_n2_census_contains_no_non_merged_closed_tuples": (
            "pre-normalization removes non-merged closed tuples from the census"
        ),
        "test_normalize_n2_conductor_stage_planning_closed": (
            "closed conductor-stage:planning rows normalize before mapping"
        ),
        "test_normalize_n2_does_not_fire_on_merged_lifecycle": (
            "N2 closed normalization does not rewrite merged lifecycle rows"
        ),
        "test_normalize_n2_expanding_closed": ("closed expanding rows normalize before mapping"),
        "test_normalize_n2_holistic_review_closed": (
            "closed holistic_review rows normalize before mapping"
        ),
        "test_normalize_n2_in_development_closed": (
            "closed in_development rows normalize before mapping"
        ),
        "test_normalize_n2_merging_closed": ("closed merging rows normalize before mapping"),
        "test_normalize_n2_open_closed": "closed open rows normalize before mapping",
        "test_normalize_n2_plan_review_closed": (
            "closed plan_review rows normalize before mapping"
        ),
        "test_normalize_n2_pr_closed": "closed pr rows normalize before mapping",
        "test_normalize_n2_preserves_closed_at": ("N2 normalization preserves existing closed_at"),
        "test_normalize_n2_test_arch_closed": ("closed test_arch rows normalize before mapping"),
        "test_null_legacy_caps_stay_null_post_backfill": (
            "NULL legacy cap columns produce NULL per-stage cap overrides"
        ),
        "test_planning_and_test_arch_get_null_caps_inherit_registry_default": (
            "planning and test_arch inherit registry caps with NULL row overrides"
        ),
        "test_planning_round_label_populates_review_round_count": (
            "planning-round:N label populates planning.review_round_count"
        ),
        "test_qa_attempts_label_populates_development_review_round_count": (
            "qa-attempts:N label populates development.review_round_count"
        ),
        "test_skip_labels_dropped": "stage-:<name> skip labels are removed after backfill",
        "test_unmapped_tuple_fails_loudly": (
            "unmapped lifecycle/status tuples fail migration and name the tuple"
        ),
        "test_uses_233_seeded_defaults": (
            "migration 234 reads task_type_default_stages seeded by migration 233"
        ),
        "test_work_attempt_count_starts_zero": ("backfilled rows start work_attempt_count at zero"),
    },
    required_symbols=("gobby.storage.migrations:_backfill_task_stage_states",),
)


def test_backfill_maps_core_lifecycle_states(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)
    _insert_task(db, "open-task", lifecycle="open", status="needs_review")
    _insert_task(db, "planning-task", lifecycle="plan_review", status="needs_review")
    _insert_task(db, "test-arch-task", lifecycle="test_arch", status="review_approved")
    _insert_task(db, "development-task", lifecycle="in_development", status="review_approved")
    _insert_task(
        db,
        "merged-task",
        lifecycle="merged",
        status="closed",
        closed_at="2026-01-03T00:00:00+00:00",
    )

    _apply_234(db)

    assert _state_map(db, "open-task") == {
        "planning": "ready",
        "test_arch": "ready",
        "expansion": "ready",
        "development": "ready",
        "pr": "ready",
        "merge": "ready",
    }
    assert _state_map(db, "planning-task") == {
        "planning": "needs_review",
        "test_arch": "ready",
        "expansion": "ready",
        "development": "ready",
        "pr": "ready",
        "merge": "ready",
    }
    assert _state_map(db, "test-arch-task") == {
        "planning": "done",
        "test_arch": "done",
        "expansion": "ready",
        "development": "ready",
        "pr": "ready",
        "merge": "ready",
    }
    assert _state_map(db, "development-task") == {
        "planning": "done",
        "test_arch": "done",
        "expansion": "done",
        "development": "review_approved",
        "pr": "ready",
        "merge": "ready",
    }
    assert set(_state_map(db, "merged-task").values()) == {"done"}


def test_backfill_honors_labels_rounds_caps_and_pr_artifact(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)
    _insert_task(
        db,
        "epic-task",
        task_type="epic",
        lifecycle="holistic_review",
        status="needs_review",
        labels=["keep", "stage-:test_arch", "planning-round:3", "qa-attempts:5"],
    )
    db.execute(
        """
        INSERT INTO task_artifacts (
            task_id, max_expansion_attempts, max_qa_rounds, max_merge_attempts,
            max_holistic_rounds, max_review_rounds, pr_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("epic-task", 2, 4, 6, 7, 8, "https://example.test/pr/1"),
    )

    _apply_234(db)

    rows = {row["stage_name"]: row for row in _stage_rows(db, "epic-task")}
    assert list(rows) == [
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
    ]
    assert rows["planning"]["review_round_count"] == 3
    assert rows["development"]["review_round_count"] == 5
    assert rows["expansion"]["max_work_attempts"] == 2
    assert rows["development"]["max_review_rounds"] == 4
    assert rows["merge"]["max_work_attempts"] == 6
    assert rows["holistic_qa"]["max_review_rounds"] == 7
    assert rows["pr"]["max_review_rounds"] == 8
    assert json.loads(rows["pr"]["artifact_refs"]) == {"pr_url": "https://example.test/pr/1"}

    labels = db.fetchone("SELECT labels FROM tasks WHERE id = ?", ("epic-task",))["labels"]
    assert json.loads(labels) == ["keep", "planning-round:3", "qa-attempts:5"]


def test_backfill_conductor_override_and_escalation_normalization(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)
    _insert_task(
        db,
        "planning-anchor",
        task_type="task",
        lifecycle="open",
        status="needs_review",
        labels=["conductor-stage:planning", "stage-:development", "keep"],
    )
    _insert_task(db, "escalated-pr", lifecycle="pr", status="escalated")

    _apply_234(db)

    assert _state_map(db, "planning-anchor") == {"planning": "needs_review"}
    planning_row = _stage_rows(db, "planning-anchor")[0]
    assert planning_row["review_policy"] == "required"
    assert planning_row["reviewer_agent"] == "plan-adversary"
    labels = db.fetchone("SELECT labels FROM tasks WHERE id = ?", ("planning-anchor",))["labels"]
    assert json.loads(labels) == ["keep"]

    assert _state_map(db, "escalated-pr")["pr"] == "in_progress"
    assert (
        db.fetchone("SELECT is_escalated FROM tasks WHERE id = 'escalated-pr'")["is_escalated"] == 1
    )


def test_backfill_close_pass_closes_all_done_open_tasks(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)
    _insert_task(db, "merged-open", lifecycle="merged", status="in_progress")

    _apply_234(db)

    task = db.fetchone(
        "SELECT closed_at, closed_in_session_id FROM tasks WHERE id = ?",
        ("merged-open",),
    )
    assert task["closed_at"] is not None
    assert task["closed_in_session_id"] == "migration:234"


def test_backfill_unmapped_tuple_fails_loudly(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)
    _insert_task(db, "unknown-status", lifecycle="in_development", status="paused")

    with pytest.raises(RuntimeError, match=r"\('in_development', 'paused'\)"):
        _apply_234(db)
