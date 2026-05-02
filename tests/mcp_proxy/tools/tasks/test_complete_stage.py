"""Phase 2 red contracts for complete_stage MCP tool behavior."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_policy_none_direct_complete": (
            "complete_stage permits direct completion for policy-none in_progress rows"
        ),
        "test_policy_required_complete_from_review_approved": (
            "complete_stage permits required-policy rows only from review_approved"
        ),
        "test_policy_required_direct_complete_rejected": (
            "complete_stage rejects required-policy in_progress rows without override"
        ),
        "test_validation_override_allows_direct_complete_on_required": (
            "validation_override_reason permits audited direct completion for required-policy rows"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
