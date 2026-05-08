"""Legacy lifecycle MCP tools are removed in Phase 5."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import LEGACY_REVIEW_TOOLS

pytestmark = pytest.mark.unit


def test_tools_absent(task_registry) -> None:
    for tool_name in LEGACY_REVIEW_TOOLS:
        assert task_registry.get_schema(tool_name) is None
