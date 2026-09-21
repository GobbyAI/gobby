"""Tests for internal MCP tool read-only classification."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills import create_skills_registry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.tasks import LocalTaskManager

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


TASK_READ_ONLY_TOOLS = {
    "get_task",
    "list_tasks",
    "search_tasks",
    "get_session_tasks",
    "get_task_sessions",
    "get_dependency_tree",
    "check_dependency_cycles",
    "list_ready_tasks",
    "list_blocked_tasks",
    "suggest_next_task",
    "get_task_stages",
    "list_stages_registry",
    "get_task_type_defaults",
    "get_build_status",
    "explain_dispatch",
    "list_build_history",
    "get_task_diff",
    "inspect_task_path_ownership",
}

TASK_OPS_READ_ONLY_TOOLS = {
    "get_expansion_run",
    "get_latest_expansion_run",
    "check_expansion_qa_result",
    "get_affected_files",
    "find_file_overlaps",
    "get_artifacts",
}

SKILLS_READ_ONLY_TOOLS = {
    "get_skill_file",
    "get_skill_files",
    "list_hubs",
    "list_skills",
    "search_hub",
}


def _read_only_tool_names(registry: InternalToolRegistry) -> set[str]:
    return {
        tool["name"]
        for tool in registry.list_tools()
        if (metadata := registry.get_tool_metadata(tool["name"])) is not None and metadata.read_only
    }


def test_read_only_metadata_is_not_advertised() -> None:
    registry = InternalToolRegistry("gobby-test")
    registry.register(
        name="inspect",
        description="Inspect state.",
        input_schema={"type": "object", "properties": {}},
        func=lambda: None,
        read_only=True,
    )
    registry.register(
        name="mutate",
        description="Mutate state.",
        input_schema={"type": "object", "properties": {}},
        func=lambda: None,
    )

    inspect_metadata = registry.get_tool_metadata("inspect")
    mutate_metadata = registry.get_tool_metadata("mutate")
    schema = registry.get_schema("inspect")
    assert inspect_metadata is not None and inspect_metadata.read_only is True
    assert mutate_metadata is not None and mutate_metadata.read_only is False
    assert schema is not None and "read_only" not in schema
    assert all("read_only" not in tool for tool in registry.list_tools())


def test_task_tool_read_only_classification(task_registry: InternalToolRegistry) -> None:
    assert _read_only_tool_names(task_registry) == TASK_READ_ONLY_TOOLS


def test_task_ops_read_only_classification_is_exact(temp_db: HubDatabase) -> None:
    registry = create_task_ops_registry(LocalTaskManager(temp_db))

    assert _read_only_tool_names(registry) == TASK_OPS_READ_ONLY_TOOLS


def test_skills_read_only_classification_is_exact(temp_db: HubDatabase) -> None:
    registry = create_skills_registry(temp_db)

    assert _read_only_tool_names(registry) == SKILLS_READ_ONLY_TOOLS
