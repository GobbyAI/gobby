"""The memory write tools advertise and enforce their content and rationale caps (#21462).

`@registry.tool` derives schemas from annotations only, so the 3,000-character
content cap and the 500-character rationale cap were invisible until a write was
rejected — and an over-length rationale was misreported as a *missing* one. The
tools now patch both limits into their schemas, reject over-cap content before
any embedding probe runs, and name the length problem in the error.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.memory_write import RATIONALE_MAX_CHARS
from gobby.memory.services.lifecycle import MAX_MEMORY_CONTENT_CHARS

pytestmark = pytest.mark.unit

_VALID_RATIONALE = "Durable convention future sessions would otherwise rediscover by hand."
_OVER_CAP_CONTENT = "x" * (MAX_MEMORY_CONTENT_CHARS + 1)
_OVER_CAP_RATIONALE = "r" * (RATIONALE_MAX_CHARS + 100)


class _StoredMemory:
    def __init__(self) -> None:
        self.id = "mem-123"
        self.content = "body"
        self.memory_type = "fact"
        self.project_id = "project-a"
        self.is_global = False
        self.updated_at = "2026-09-01T00:00:00+00:00"
        self.rationale = _VALID_RATIONALE


@pytest.fixture
def manager() -> MagicMock:
    stub = MagicMock()
    stub.create_memory = AsyncMock(return_value=_StoredMemory())
    stub.search_memories = AsyncMock(return_value=[])
    stub.update_memory_scoped = AsyncMock(return_value=_StoredMemory())
    stub.resolve_memory_id = MagicMock(side_effect=lambda ref, project_id=None: ref)
    stub.db = MagicMock()
    return stub


@pytest.fixture
def registry(manager: MagicMock) -> InternalToolRegistry:
    return create_memory_registry(lambda: manager)


def _schema(registry: InternalToolRegistry, tool: str) -> dict[str, Any]:
    meta = registry.get_tool_metadata(tool)
    assert meta is not None
    return meta.input_schema


class TestSchemaAdvertisesCaps:
    @pytest.mark.parametrize("tool", ["create_memory", "update_memory"])
    def test_content_and_rationale_carry_max_length(
        self, registry: InternalToolRegistry, tool: str
    ) -> None:
        properties = _schema(registry, tool)["properties"]

        assert properties["content"]["maxLength"] == MAX_MEMORY_CONTENT_CHARS
        assert properties["rationale"]["maxLength"] == RATIONALE_MAX_CHARS
        assert str(MAX_MEMORY_CONTENT_CHARS) in properties["content"]["description"]
        assert "rejected" in properties["content"]["description"]

    def test_create_memory_requires_rationale(self, registry: InternalToolRegistry) -> None:
        schema = _schema(registry, "create_memory")

        assert "rationale" in schema["required"]
        assert "content" in schema["required"]

    def test_update_memory_states_conditional_rationale(
        self, registry: InternalToolRegistry
    ) -> None:
        properties = _schema(registry, "update_memory")["properties"]

        description = properties["rationale"]["description"].lower()
        assert "required when content" in description

    def test_docstrings_state_the_content_cap(self, registry: InternalToolRegistry) -> None:
        for tool in ("create_memory", "update_memory"):
            meta = registry.get_tool_metadata(tool)
            assert meta is not None
            doc = meta.func.__doc__ or ""
            assert str(MAX_MEMORY_CONTENT_CHARS) in doc, tool


class TestOverCapContentIsRejected:
    @pytest.mark.asyncio
    async def test_create_memory_rejects_before_probing(
        self, registry: InternalToolRegistry, manager: MagicMock
    ) -> None:
        result = await registry.call(
            "create_memory",
            {"content": _OVER_CAP_CONTENT, "rationale": _VALID_RATIONALE},
        )

        assert result["success"] is False
        error = str(result["error"])
        assert error.startswith("content_too_long:")
        assert str(MAX_MEMORY_CONTENT_CHARS + 1) in error
        assert str(MAX_MEMORY_CONTENT_CHARS) in error
        manager.search_memories.assert_not_awaited()
        manager.create_memory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_memory_rejects_over_cap_content(
        self, registry: InternalToolRegistry, manager: MagicMock
    ) -> None:
        result = await registry.call(
            "update_memory",
            {"memory_id": "mem-123", "content": _OVER_CAP_CONTENT, "rationale": _VALID_RATIONALE},
        )

        assert result["success"] is False
        error = str(result["error"])
        assert error.startswith("content_too_long:")
        assert str(MAX_MEMORY_CONTENT_CHARS + 1) in error
        manager.update_memory_scoped.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_content_at_cap_is_written_untouched(
        self, registry: InternalToolRegistry, manager: MagicMock
    ) -> None:
        body = "y" * MAX_MEMORY_CONTENT_CHARS

        with patch("gobby.utils.project_context.get_project_context") as ctx:
            ctx.return_value = {"id": "project-a", "name": "Project A"}
            result = await registry.call(
                "update_memory",
                {"memory_id": "mem-123", "content": body, "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is True, result
        assert manager.update_memory_scoped.call_args.kwargs["content"] == body


class TestRationaleErrorsAreDistinct:
    @pytest.mark.asyncio
    async def test_update_without_rationale_still_reports_required(
        self, registry: InternalToolRegistry, manager: MagicMock
    ) -> None:
        result = await registry.call("update_memory", {"memory_id": "mem-123", "content": "new"})

        assert result["success"] is False
        assert str(result["error"]).startswith("rationale_required:")
        manager.update_memory_scoped.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("create_memory", {"content": "new"}),
            ("update_memory", {"memory_id": "mem-123", "content": "new"}),
            ("update_memory", {"memory_id": "mem-123"}),
        ],
        ids=["create", "update-content", "update-rationale-only"],
    )
    async def test_over_length_rationale_names_the_length(
        self,
        registry: InternalToolRegistry,
        manager: MagicMock,
        tool: str,
        arguments: dict[str, Any],
    ) -> None:
        result = await registry.call(tool, {**arguments, "rationale": _OVER_CAP_RATIONALE})

        assert result["success"] is False
        error = str(result["error"])
        assert error.startswith("rationale_too_long:")
        assert str(RATIONALE_MAX_CHARS) in error
        assert str(len(_OVER_CAP_RATIONALE)) in error
        manager.create_memory.assert_not_awaited()
        manager.update_memory_scoped.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tags_only_update_needs_no_rationale(
        self, registry: InternalToolRegistry, manager: MagicMock
    ) -> None:
        with patch("gobby.utils.project_context.get_project_context") as ctx:
            ctx.return_value = {"id": "project-a", "name": "Project A"}
            result = await registry.call("update_memory", {"memory_id": "mem-123", "tags": ["k"]})

        assert result["success"] is True, result
        manager.update_memory_scoped.assert_awaited_once()
