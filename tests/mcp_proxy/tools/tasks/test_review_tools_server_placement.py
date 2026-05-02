"""Phase 2 red contracts for review-tool MCP placement."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_bundled_agents_reference_gobby_tasks_review_tools": (
            "bundled agents continue to route review tools to gobby-tasks"
        ),
        "test_review_tool_signatures_unchanged_post_rewire": (
            "review MCP tool signatures remain agent-compatible after stage-native rewire"
        ),
        "test_review_tools_absent_from_gobby_tasks_ops": (
            "review tools are absent from gobby-tasks-ops"
        ),
        "test_review_tools_on_gobby_tasks_only": (
            "review tools remain on gobby-tasks read/lifecycle server"
        ),
    },
    required_text={
        "src/gobby/mcp_proxy/tools/tasks/_factory.py": (
            "mark_task_needs_review",
            "mark_task_review_approved",
            "mark_task_review_rejected",
        )
    },
)
