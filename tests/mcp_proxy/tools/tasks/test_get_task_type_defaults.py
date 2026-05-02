"""Phase 2 red contracts for get_task_type_defaults MCP tool."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_known_and_unknown_types": (
            "get_task_type_defaults returns configured manifests for known task types and "
            "a typed error for unknown types"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_read:create_stage_read_registry",),
)
