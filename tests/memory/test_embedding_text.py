"""The vector text contract: content plus rationale, one helper for every embed site."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.memory.embedding_text import memory_embedding_text
from gobby.memory.services.lifecycle import MemoryLifecycleService
from gobby.storage.memories import Memory, MemoryType

pytestmark = pytest.mark.unit


def _memory(content: str, rationale: str | None) -> Memory:
    return Memory(
        id="00000000-0000-4000-8000-000000000001",
        memory_type=MemoryType.FACT,
        content=content,
        created_at=datetime(2026, 5, 31, tzinfo=UTC),
        updated_at=datetime(2026, 5, 31, tzinfo=UTC),
        rationale=rationale,
    )


def test_embedding_text_appends_rationale_after_a_why_marker() -> None:
    assert memory_embedding_text("Body", "Needed later.") == "Body\n\nWhy: Needed later."


@pytest.mark.parametrize("rationale", [None, "", "   "])
def test_embedding_text_is_bare_content_without_a_rationale(rationale: str | None) -> None:
    assert memory_embedding_text("Body", rationale) == "Body"


def test_embedding_text_strips_rationale_whitespace_only() -> None:
    assert memory_embedding_text("Body ", "  Why.  ") == "Body \n\nWhy: Why."


def _lifecycle_recording_index_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MemoryLifecycleService, list[tuple[str, Memory | None, Memory | None]]]:
    """A bare lifecycle whose two index-rebuild paths append to ``calls`` instead."""
    service = MemoryLifecycleService.__new__(MemoryLifecycleService)
    calls: list[tuple[str, Memory | None, Memory | None]] = []

    async def refresh(*, old_memory: Memory | None, memory: Memory) -> None:
        calls.append(("refresh", old_memory, memory))

    async def reconcile(memory: Memory, **_kwargs: Any) -> bool:
        calls.append(("reconcile", None, memory))
        return True

    monkeypatch.setattr(service, "_refresh_content_indices", refresh)
    monkeypatch.setattr(service, "_reconcile_active_snapshot", reconcile)
    return service, calls


@pytest.mark.asyncio
async def test_rationale_only_update_refreshes_content_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vector embeds content and rationale, so a rationale edit re-embeds (#21010)."""
    service, calls = _lifecycle_recording_index_calls(monkeypatch)
    old = _memory("Body", "Old why.")
    new = _memory("Body", "New why.")

    await service._sync_updated_indices(old, new)

    assert calls == [("refresh", old, new)]


@pytest.mark.asyncio
async def test_unchanged_text_does_not_refresh_content_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, calls = _lifecycle_recording_index_calls(monkeypatch)
    old = _memory("Body", "Why.")
    new = _memory("Body", "Why.")

    await service._sync_updated_indices(old, new)

    assert calls == []


@pytest.mark.asyncio
async def test_type_only_update_refreshes_payload_not_the_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, calls = _lifecycle_recording_index_calls(monkeypatch)
    old = _memory("Body", "Why.")
    new = _memory("Body", "Why.")
    new.memory_type = MemoryType.PATTERN

    await service._sync_updated_indices(old, new)

    assert calls == [("reconcile", None, new)]
