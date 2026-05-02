"""Phase 2 red contracts for stage-native review tools."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_approved_advances_to_review_approved_on_same_row": (
            "mark_task_review_approved transitions current stage needs_review to review_approved"
        ),
        "test_approved_calls_approve_review_no_legacy_writes": (
            "approval calls StageStatesManager.approve_review without writing legacy statuses"
        ),
        "test_approved_does_not_advance_to_next_stage": (
            "approval leaves next-stage advancement to the dispatcher"
        ),
        "test_approved_rejected_on_policy_none": (
            "approval raises IllegalStageTransitionError for policy-none rows"
        ),
        "test_needs_review_calls_submit_for_review_no_legacy_writes": (
            "needs-review calls submit_for_review without writing legacy statuses"
        ),
        "test_needs_review_rejected_on_policy_none": (
            "needs-review raises IllegalStageTransitionError for policy-none rows"
        ),
        "test_needs_review_submits_for_review_on_same_row": (
            "mark_task_needs_review transitions current stage in_progress to needs_review"
        ),
        "test_no_current_stage_errors": (
            "review tools raise NoCurrentStageError on exhausted manifests"
        ),
        "test_rejected_calls_reject_review_no_legacy_writes": (
            "rejection calls reject_review without writing legacy statuses"
        ),
        "test_rejected_does_not_increment_work_attempts": ("rejection increments no work attempts"),
        "test_rejected_over_cap_escalates": (
            "rejection escalates when review_round_count meets effective cap"
        ),
        "test_rejected_returns_to_ready_increments_review_rounds": (
            "rejection returns same row to ready and increments review_round_count"
        ),
        "test_wrong_source_state_errors_no_mutation": (
            "review tools raise transition errors and mutate nothing from wrong source states"
        ),
    },
    required_symbols=("gobby.storage.tasks._stage_states:StageStatesManager",),
)
