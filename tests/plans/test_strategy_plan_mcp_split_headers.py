"""Phase 2 red contracts for MCP split documentation."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_ops_header_lists_only_mutating_tools": (
            "the strategy plan ops-header lists only mutating stage tools"
        ),
        "test_read_header_lists_only_read_tools": (
            "the strategy plan read-header lists only non-mutating stage tools"
        ),
        "test_strategy_plan_has_separate_read_and_ops_headers": (
            "the strategy plan documents distinct gobby-tasks and gobby-tasks-ops stage surfaces"
        ),
    },
    required_text={
        ".gobby/plans/task-13482-stage-manifest-cutover.md": (
            "gobby-tasks-ops",
            "gobby-tasks",
        )
    },
)
