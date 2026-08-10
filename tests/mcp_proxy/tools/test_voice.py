"""Tests for gobby-voice MCP tool registry (Whisper custom vocabulary)."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.runtime_models import ConfigSnapshot
from gobby.mcp_proxy.tools.voice import create_voice_registry

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(
    vocab: list[str] | None = None,
    whisper_prompt: str = "Gobby",
) -> tuple[Any, MagicMock]:
    """Create a voice registry with mocked dependencies."""
    voice_config: dict[str, object] = {"whisper_prompt": whisper_prompt}
    if vocab is not None:
        voice_config["whisper_vocabulary"] = vocab
    config = DaemonConfig(voice=voice_config)

    runtime = MagicMock()
    runtime.snapshot = ConfigSnapshot(
        revision=7,
        desired=config,
        active=config,
        row_revisions={},
        pending_restart_keys=frozenset(),
        failed_live_keys={},
        desired_values={},
        active_values={},
    )
    service = MagicMock()
    service.runtime = runtime

    async def patch_flat(*, expected_revision: int, values: dict[str, object]) -> None:
        snapshot = runtime.snapshot
        assert expected_revision == snapshot.revision
        terms = values["voice.whisper_vocabulary"]
        assert isinstance(terms, list) and all(isinstance(term, str) for term in terms)
        config_values = snapshot.active.model_dump(mode="python")
        config_values["voice"]["whisper_vocabulary"] = terms
        updated = DaemonConfig(**config_values)
        runtime.snapshot = ConfigSnapshot(
            revision=snapshot.revision + 1,
            desired=updated,
            active=updated,
            row_revisions={},
            pending_restart_keys=frozenset(),
            failed_live_keys={},
            desired_values={},
            active_values={},
        )

    service.patch_flat = AsyncMock(side_effect=patch_flat)
    registry = create_voice_registry(lambda: service)
    return registry, service


async def _call_tool(registry: Any, name: str, **kwargs: Any) -> dict[str, Any]:
    """Call a tool on the registry by name."""
    tool = registry.get_tool(name)
    assert tool is not None, f"Tool '{name}' not found"
    result = tool(**kwargs)
    assert isinstance(result, Awaitable)
    resolved = await result
    assert isinstance(resolved, dict)
    return cast(dict[str, Any], resolved)


# ---------------------------------------------------------------------------
# add_vocab
# ---------------------------------------------------------------------------


class TestAddVocab:
    async def test_add_single_term(self) -> None:
        registry, service = _make_registry(vocab=["Gobby"])
        result = await _call_tool(registry, "add_vocab", terms="Kubernetes")
        assert result["success"] is True
        assert result["added"] == ["Kubernetes"]
        assert result["total"] == 2
        service.patch_flat.assert_awaited_once()

    async def test_add_multiple_terms(self) -> None:
        registry, _ = _make_registry(vocab=[])
        result = await _call_tool(registry, "add_vocab", terms="FastAPI, Pydantic, Redis")
        assert result["success"] is True
        assert result["added"] == ["FastAPI", "Pydantic", "Redis"]
        assert result["total"] == 3

    async def test_dedup_case_insensitive(self) -> None:
        registry, _ = _make_registry(vocab=["Gobby", "MCP"])
        result = await _call_tool(registry, "add_vocab", terms="gobby, NewTerm")
        assert result["success"] is True
        assert result["added"] == ["NewTerm"]
        assert result["already_existed"] == 1
        assert result["total"] == 3

    async def test_all_duplicates(self) -> None:
        registry, service = _make_registry(vocab=["Gobby", "MCP"])
        result = await _call_tool(registry, "add_vocab", terms="gobby, mcp")
        assert result["success"] is True
        assert result["added"] == []
        assert result["already_existed"] == 2
        service.patch_flat.assert_not_awaited()

    async def test_empty_terms(self) -> None:
        registry, _ = _make_registry(vocab=[])
        result = await _call_tool(registry, "add_vocab", terms="  ,  , ")
        assert result["success"] is False
        assert "No valid terms" in result["error"]

    async def test_dedup_within_input(self) -> None:
        registry, _ = _make_registry(vocab=[])
        result = await _call_tool(registry, "add_vocab", terms="FastAPI, fastapi, FASTAPI")
        assert result["success"] is True
        assert result["added"] == ["FastAPI"]
        assert result["total"] == 1


# ---------------------------------------------------------------------------
# remove_vocab
# ---------------------------------------------------------------------------


class TestRemoveVocab:
    async def test_remove_existing(self) -> None:
        registry, service = _make_registry(vocab=["Gobby", "MCP", "FastAPI"])
        result = await _call_tool(registry, "remove_vocab", terms="MCP")
        assert result["success"] is True
        assert result["removed"] == 1
        assert result["total"] == 2
        service.patch_flat.assert_awaited_once()

    async def test_remove_case_insensitive(self) -> None:
        registry, _ = _make_registry(vocab=["Gobby", "MCP"])
        result = await _call_tool(registry, "remove_vocab", terms="gobby")
        assert result["success"] is True
        assert result["removed"] == 1
        assert result["total"] == 1

    async def test_remove_missing(self) -> None:
        registry, service = _make_registry(vocab=["Gobby"])
        result = await _call_tool(registry, "remove_vocab", terms="NonExistent")
        assert result["success"] is True
        assert result["removed"] == 0
        assert result["not_found"] == 1
        service.patch_flat.assert_not_awaited()

    async def test_empty_terms(self) -> None:
        registry, _ = _make_registry(vocab=["Gobby"])
        result = await _call_tool(registry, "remove_vocab", terms="  ,  ")
        assert result["success"] is False
        assert "No valid terms" in result["error"]


# ---------------------------------------------------------------------------
# list_vocab
# ---------------------------------------------------------------------------


class TestListVocab:
    async def test_list_empty(self) -> None:
        registry, _ = _make_registry(vocab=[], whisper_prompt="")
        result = await _call_tool(registry, "list_vocab")
        assert result["success"] is True
        assert result["vocabulary"] == []
        assert result["count"] == 0
        assert result["whisper_prompt"] == ""

    async def test_list_populated(self) -> None:
        registry, _ = _make_registry(
            vocab=["Gobby", "MCP", "FastAPI"],
            whisper_prompt="Gobby",
        )
        result = await _call_tool(registry, "list_vocab")
        assert result["success"] is True
        assert result["vocabulary"] == ["Gobby", "MCP", "FastAPI"]
        assert result["count"] == 3
        assert result["whisper_prompt"] == "Gobby"


# ---------------------------------------------------------------------------
# clear_vocab
# ---------------------------------------------------------------------------


class TestClearVocab:
    async def test_clear_populated(self) -> None:
        registry, service = _make_registry(vocab=["Gobby", "MCP"])
        result = await _call_tool(registry, "clear_vocab")
        assert result["success"] is True
        assert result["cleared"] == 2
        service.patch_flat.assert_awaited_once()

    async def test_clear_already_empty(self) -> None:
        registry, _ = _make_registry(vocab=[])
        result = await _call_tool(registry, "clear_vocab")
        assert result["success"] is True
        assert result["cleared"] == 0

    async def test_list_after_clear(self) -> None:
        registry, _ = _make_registry(vocab=["Gobby", "MCP"])
        await _call_tool(registry, "clear_vocab")
        result = await _call_tool(registry, "list_vocab")
        assert result["vocabulary"] == []
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# Persistence through revisioned config service
# ---------------------------------------------------------------------------


class TestPersistence:
    async def test_add_uses_snapshot_revision(self) -> None:
        registry, service = _make_registry(vocab=["Gobby"])
        result = await _call_tool(registry, "add_vocab", terms="NewTerm")

        assert result == {
            "success": True,
            "added": ["NewTerm"],
            "already_existed": 0,
            "total": 2,
        }
        service.patch_flat.assert_awaited_once_with(
            expected_revision=7,
            values={"voice.whisper_vocabulary": ["Gobby", "NewTerm"]},
        )

    async def test_remove_uses_snapshot_revision(self) -> None:
        registry, service = _make_registry(vocab=["Gobby", "MCP"])
        result = await _call_tool(registry, "remove_vocab", terms="MCP")

        assert result == {"success": True, "removed": 1, "not_found": 0, "total": 1}
        service.patch_flat.assert_awaited_once_with(
            expected_revision=7,
            values={"voice.whisper_vocabulary": ["Gobby"]},
        )

    async def test_clear_uses_snapshot_revision(self) -> None:
        registry, service = _make_registry(vocab=["Gobby"])
        result = await _call_tool(registry, "clear_vocab")

        assert result == {"success": True, "cleared": 1}
        service.patch_flat.assert_awaited_once_with(
            expected_revision=7,
            values={"voice.whisper_vocabulary": []},
        )
