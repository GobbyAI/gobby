"""Legacy lifecycle MCP tools are removed in Phase 5."""

from __future__ import annotations

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from tests.phase5_contract_helpers import LEGACY_REVIEW_TOOLS

pytestmark = pytest.mark.unit


def test_tools_absent(task_registry: InternalToolRegistry) -> None:
    for tool_name in LEGACY_REVIEW_TOOLS:
        assert task_registry.get_schema(tool_name) is None


def test_generic_release_or_unclaim_tools_absent(task_registry: InternalToolRegistry) -> None:
    for tool_name in ("release_claim", "unclaim_task", "unclaim"):
        assert task_registry.get_schema(tool_name) is None


def test_coordinator_release_requires_named_holder_and_reason(
    task_registry: InternalToolRegistry,
) -> None:
    schema = task_registry.get_schema("release_task_claim")
    assert schema is not None
    assert set(schema["inputSchema"]["required"]) == {"task_id", "holder_session_ref", "reason"}
