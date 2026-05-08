"""Phase 2 red contracts for stage MCP tool registration."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_mutating_tools_absent_from_gobby_tasks": (
            "mutating stage tools are absent from gobby-tasks"
        ),
        "test_mutating_tools_visible_on_gobby_tasks_ops": (
            "mutating stage tools are registered on gobby-tasks-ops"
        ),
        "test_read_tools_absent_from_gobby_tasks_ops": (
            "read-only stage tools are absent from gobby-tasks-ops"
        ),
        "test_read_tools_visible_on_gobby_tasks": (
            "read-only stage tools are registered on gobby-tasks"
        ),
    },
    required_symbols=(
        "gobby.mcp_proxy.tools.tasks._stage_read:create_stage_read_registry",
        "gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",
    ),
)
