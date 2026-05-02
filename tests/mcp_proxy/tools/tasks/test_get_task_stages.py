"""Phase 2 red contracts for get_task_stages MCP tool."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {"test_returns_position_order": "get_task_stages returns manifest rows in position order"},
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_read:create_stage_read_registry",),
)
