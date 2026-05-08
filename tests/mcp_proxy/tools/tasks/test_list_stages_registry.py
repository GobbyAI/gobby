"""Phase 2 red contracts for list_stages_registry MCP tool."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_omits_dropped_review_stages": (
            "list_stages_registry omits dropped review-only stages"
        ),
        "test_returns_all_11": "list_stages_registry returns the canonical runtime stages",
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_read:create_stage_read_registry",),
)
