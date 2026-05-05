"""Phase 2 red contracts for MCP build stage caps."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_build_task_forwards_to_shared_service": (
            "MCP build_task forwards --stage settings through the shared build service"
        ),
        "test_inputschema_excludes_max_expansion_attempts": (
            "MCP build_task schema removes max_expansion_attempts"
        ),
        "test_inputschema_excludes_max_holistic_rounds": (
            "MCP build_task schema removes max_holistic_rounds"
        ),
        "test_inputschema_excludes_max_merge_attempts": (
            "MCP build_task schema removes max_merge_attempts"
        ),
        "test_inputschema_excludes_max_qa_rounds": "MCP build_task schema removes max_qa_rounds",
        "test_inputschema_excludes_max_review_rounds": (
            "MCP build_task schema removes max_review_rounds"
        ),
        "test_stage_caps_array_property_present": (
            "MCP build_task schema exposes the stage string array"
        ),
    },
    required_symbols=("gobby.mcp_proxy.tools.build:StageCapOverride",),
)
