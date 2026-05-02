"""Phase 2 red contracts for shared stage MCP tool behavior."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_start_stage_skipping_errors": (
            "start_stage rejects attempts to skip earlier ready manifest rows"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.tasks._stage_ops:create_stage_ops_registry",),
)
