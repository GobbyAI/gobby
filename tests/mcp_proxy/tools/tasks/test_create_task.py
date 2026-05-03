"""MCP creation surface contracts for Phase 5 task types."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_simple_fix_type(task_registry) -> None:
    schema = task_registry.get_schema("create_task")
    task_type_schema = schema["inputSchema"]["properties"]["task_type"]

    assert "simple_fix" in task_type_schema.get("enum", [])
    assert "review_anchor" in task_type_schema.get("enum", [])
