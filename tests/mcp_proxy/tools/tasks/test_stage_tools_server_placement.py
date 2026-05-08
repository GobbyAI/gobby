"""Phase 2 red contracts for stage MCP server placement."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_bundled_agents_route_mutating_tools_to_gobby_tasks_ops": (
            "bundled agents route mutating stage tools through gobby-tasks-ops"
        ),
        "test_bundled_agents_route_read_tools_to_gobby_tasks": (
            "bundled agents route read stage tools through gobby-tasks"
        ),
        "test_mutating_tools_only_on_gobby_tasks_ops": (
            "mutating stage tools only register on gobby-tasks-ops"
        ),
        "test_no_cross_server_leakage_in_either_direction": (
            "stage read and mutating tools do not leak across MCP server factories"
        ),
        "test_read_tools_only_on_gobby_tasks": (
            "read-only stage tools only register on gobby-tasks"
        ),
        "test_strategy_plan_and_implementation_plan_agree_on_split": (
            "strategy and implementation plans agree on the stage MCP read/ops split"
        ),
    },
    required_symbols=(
        "gobby.mcp_proxy.tools.tasks._stage_read:create_stage_read_registry",
        "gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",
    ),
)
