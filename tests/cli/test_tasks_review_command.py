"""Phase 2 red contracts for gobby tasks review."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_approve_advances_to_review_approved": (
            "review --approve calls approve_review on the current stage"
        ),
        "test_reject_requires_reason": "review --reject requires an explicit reason",
        "test_reject_returns_to_ready_increments_review_rounds": (
            "review --reject calls reject_review and increments the review-round counter"
        ),
        "test_review_on_policy_none_errors_with_payload": (
            "review commands surface IllegalStageTransitionError payloads for policy-none rows"
        ),
        "test_submit_advances_to_needs_review": (
            "review --submit calls submit_for_review on the current stage"
        ),
    },
    required_paths=("src/gobby/cli/tasks/review.py",),
    required_symbols=("gobby.storage.tasks._stage_states:StageStatesManager",),
)
