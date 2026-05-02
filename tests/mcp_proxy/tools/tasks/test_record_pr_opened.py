"""Phase 2 red contracts for record_pr_opened MCP tool."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_idempotent": "record_pr_opened can be called repeatedly for the same PR URL",
        "test_no_stage_change_idempotent": (
            "record_pr_opened stores PR metadata without mutating the current stage"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
