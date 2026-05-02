"""Phase 2 red contracts for record_merge_result MCP placement."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_absent_from_gobby_tasks": "record_merge_result is absent from the read server",
        "test_registered_with_stub_on_gobby_tasks_ops": (
            "record_merge_result is registered on gobby-tasks-ops during Phase 2"
        ),
        "test_stub_raises_notimplemented": (
            "Phase 2 record_merge_result stub raises NotImplementedError until Phase 4.2"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
