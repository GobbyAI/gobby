"""Phase 2 red contracts for start_stage MCP tool behavior."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_increments_work_attempt_count": (
            "start_stage increments work_attempt_count and no review counter"
        ),
        "test_transitions_ready_to_in_progress": (
            "start_stage transitions the current ready row to in_progress"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
