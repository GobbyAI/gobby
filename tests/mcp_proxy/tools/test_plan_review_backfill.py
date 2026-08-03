from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.review_learning.service import ReviewLearningService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from tests.review_coverage_helpers import StubReviewLearningService
from tests.review_learning.test_round_diff import (
    DurableLineage,
    _create_durable_lineage,
    _persist_round,
    _plan_text,
)
from tests.storage.tasks._stage_test_helpers import set_stage_state


@pytest.mark.asyncio
async def test_backfill_wire_contract(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    durable_lineage_fixture = _create_durable_lineage(temp_db, tmp_path)
    recorder = StubReviewLearningService(fail=True)
    ctx = RegistryContext(
        task_manager=durable_lineage_fixture.manager,
        review_learning_service=cast(ReviewLearningService, recorder),
    )
    registry = create_stage_ops_registry(ctx)
    arguments = {
        "task_id": durable_lineage_fixture.task_id,
        "stage": durable_lineage_fixture.stage,
    }

    failed = await registry.call("backfill_plan_review_lessons", arguments)
    assert failed["lesson_mint_status"] == "failed"
    recorder.fail = False
    minted = await registry.call("backfill_plan_review_lessons", arguments)
    assert minted["lesson_mint_status"] == "minted"
    assert minted["minted_lesson_ids"] == ["lesson-1"]
    replay = await registry.call("backfill_plan_review_lessons", arguments)
    assert replay == minted
    assert len(recorder.calls) == 2

    project_id = durable_lineage_fixture.manager.get_task(
        durable_lineage_fixture.task_id
    ).project_id
    empty_plan = tmp_path / ".gobby" / "plans" / "empty.md"
    empty_plan.write_text(_plan_text(), encoding="utf-8")
    empty_task = durable_lineage_fixture.manager.create_task(
        project_id,
        "Empty approval lineage",
        task_type="task",
        category="planning",
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    empty_lineage = DurableLineage(
        db=temp_db,
        manager=durable_lineage_fixture.manager,
        service=durable_lineage_fixture.service,
        task_id=empty_task.id,
        stage="planning",
        session_id=durable_lineage_fixture.session_id,
        plan_path=empty_plan,
        approval_evidence_id="",
    )
    empty_lineage.approval_evidence_id = _persist_round(
        empty_lineage,
        round_number=1,
        findings=[],
        verdict="approved",
    )
    none_result = await registry.call(
        "backfill_plan_review_lessons",
        {"task_id": empty_task.id, "stage": "planning"},
    )
    assert none_result["lesson_mint_status"] == "none"
    assert (
        await registry.call(
            "backfill_plan_review_lessons",
            {"task_id": empty_task.id, "stage": "planning"},
        )
        == none_result
    )

    missing = durable_lineage_fixture.manager.create_task(
        project_id,
        "No approval checkpoint",
        task_type="task",
        category="planning",
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    refused = await registry.call(
        "backfill_plan_review_lessons",
        {"task_id": missing.id, "stage": "planning"},
    )
    assert refused["error"] == "approval_checkpoint_missing"
    for invalid_task_id in ("#999999", ""):
        invalid = await registry.call(
            "backfill_plan_review_lessons",
            {"task_id": invalid_task_id, "stage": "planning"},
        )
        assert invalid["error"] == "invalid_task_id"


@pytest.mark.asyncio
async def test_non_plan_approval_unaffected(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.plans.review_evidence import PlanReviewEvidenceService

    project = LocalProjectManager(temp_db).create(
        name="non-plan-review-paths",
        repo_path=str(tmp_path),
    )
    session = SessionManager(temp_db).register(
        external_id="non-plan-review-parent",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
    )
    manager = LocalTaskManager(temp_db)
    recorder = StubReviewLearningService()
    ctx = RegistryContext(
        task_manager=manager,
        review_learning_service=cast(ReviewLearningService, recorder),
    )
    registry = create_stage_ops_registry(ctx)
    review_surfaces = (
        ("development", "development"),
        ("expansion", "expansion"),
        ("document", "development"),
        ("epic_qa", "epic_qa"),
        ("pr", "pr"),
        ("trajectory", "pr"),
    )

    def forbid_evidence(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("non-planning review touched plan evidence")

    monkeypatch.setattr(PlanReviewEvidenceService, "authorize_current_attempt", forbid_evidence)
    monkeypatch.setattr(PlanReviewEvidenceService, "apply_plan_review_manifest", forbid_evidence)

    for index, (surface, stage) in enumerate(review_surfaces):
        facade_approve = manager.create_task(
            project.id,
            f"Facade approve {surface}",
            category="code",
            isolation="none",
            validation_criteria="Test task completion is observable.",
        )
        manager.initialize_task_manifest(facade_approve.id, stage_names=[stage])
        set_stage_state(temp_db, facade_approve.id, stage, "needs_review")
        approved = manager.approve_review(facade_approve.id, stage, approval_notes="approved")
        approved_stage = manager.stage_states.current_stage(approved.id)
        assert approved_stage is not None
        assert approved_stage.state == "review_approved"

        facade_reject = manager.create_task(
            project.id,
            f"Facade reject {surface}",
            category="code",
            isolation="none",
            validation_criteria="Test task completion is observable.",
        )
        manager.initialize_task_manifest(facade_reject.id, stage_names=[stage])
        set_stage_state(temp_db, facade_reject.id, stage, "needs_review")
        rejected = manager.reject_review(facade_reject.id, stage, rejection_notes="retry")
        rejected_stage = manager.stage_states.current_stage(rejected.id)
        assert rejected_stage is not None
        assert rejected_stage.state == "ready"

        registry_approve = manager.create_task(
            project.id,
            f"Registry approve {surface}",
            category="code",
            isolation="none",
            validation_criteria="Test task completion is observable.",
        )
        manager.initialize_task_manifest(registry_approve.id, stage_names=[stage])
        set_stage_state(temp_db, registry_approve.id, stage, "needs_review")
        with session_context_for_test(session.id):
            approval_result = await registry.call(
                "approve_review",
                {
                    "task_id": registry_approve.id,
                    "stage_name": stage,
                    "approval_notes": f"approved-{index}",
                },
            )
        assert "error" not in approval_result

        registry_reject = manager.create_task(
            project.id,
            f"Registry reject {surface}",
            category="code",
            isolation="none",
            validation_criteria="Test task completion is observable.",
        )
        manager.initialize_task_manifest(registry_reject.id, stage_names=[stage])
        set_stage_state(temp_db, registry_reject.id, stage, "needs_review")
        with session_context_for_test(session.id):
            rejection_result = await registry.call(
                "reject_review",
                {
                    "task_id": registry_reject.id,
                    "stage_name": stage,
                    "rejection_notes": f"rejected-{index}",
                },
            )
        assert "error" not in rejection_result

    planning = manager.create_task(
        project.id,
        "Planning approval requires evidence",
        task_type="task",
        category="planning",
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    manager.initialize_task_manifest(planning.id, stage_names=["planning"])
    set_stage_state(temp_db, planning.id, "planning", "needs_review")
    with session_context_for_test(session.id):
        refused = await registry.call(
            "approve_review",
            {
                "task_id": planning.id,
                "stage_name": "planning",
                "round_number": 1,
            },
        )
    assert refused["error"] == "missing_evidence_id"
    planning_stage = manager.stage_states.current_stage(planning.id)
    assert planning_stage is not None
    assert planning_stage.state == "needs_review"
    assert recorder.calls == []
