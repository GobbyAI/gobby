"""Phase 2 red contracts for record_pr_verdict MCP tool."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_approved_calls_approve_review": ("approved PR verdict delegates to approve_review"),
        "test_approved_calls_approve_review_only": (
            "approved PR verdict calls approve_review and no other stage mutator"
        ),
        "test_approved_does_not_advance_to_merge": (
            "approved PR verdict leaves advancement to the dispatcher"
        ),
        "test_approved_does_not_call_complete_stage": (
            "approved PR verdict does not call complete_stage"
        ),
        "test_approved_increments_no_counter": "approved PR verdict increments no counters",
        "test_approved_post_state_is_review_approved": (
            "approved PR verdict leaves the pr row in review_approved"
        ),
        "test_needs_changes_treated_as_rejected": (
            "needs_changes PR verdict is equivalent to reject_review"
        ),
        "test_raises_when_pr_not_in_needs_review": (
            "record_pr_verdict raises if the pr row is not needs_review"
        ),
        "test_rejected_calls_reject_review_only": (
            "rejected PR verdict calls reject_review and no work-attempt mutator"
        ),
        "test_rejected_increments_review_round_count": (
            "rejected PR verdict increments review_round_count only"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
