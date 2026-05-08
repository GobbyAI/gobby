"""Phase 2 red contracts for fail_stage MCP tool behavior."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_illegal_from_done_terminal": "fail_stage rejects done terminal rows",
        "test_illegal_from_needs_review_policy_optional": (
            "fail_stage rejects needs_review rows for optional policy"
        ),
        "test_illegal_from_needs_review_policy_required": (
            "fail_stage rejects needs_review rows for required policy"
        ),
        "test_illegal_from_ready_policy_none": "fail_stage rejects ready policy-none rows",
        "test_illegal_from_ready_policy_optional": "fail_stage rejects ready optional rows",
        "test_illegal_from_ready_policy_required": "fail_stage rejects ready required rows",
        "test_illegal_from_review_approved_policy_optional": (
            "fail_stage rejects review_approved optional rows"
        ),
        "test_illegal_from_review_approved_policy_required": (
            "fail_stage rejects review_approved required rows"
        ),
        "test_over_cap_escalates": "fail_stage escalates when work attempts meet the cap",
        "test_under_cap_returns_to_ready": (
            "fail_stage returns in_progress rows to ready without incrementing counters"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
