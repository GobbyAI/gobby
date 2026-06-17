"""Shared protocols for memory dream collaborators."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import Memory


class MemoryDreamManagerProtocol(Protocol):
    db: HubDatabase

    async def alist_memories(self, *, limit: int | None, offset: int) -> list[Any]: ...

    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        project_id: str | None = None,
        memory_type: str | None = None,
        include_global: bool = True,
    ) -> list[Any]: ...

    def mark_dreamed(
        self,
        memory_id: str,
        *,
        hidden_as: Literal["review", "delete"] | None = None,
        when: str | None = None,
    ) -> bool: ...

    async def create_memory(
        self,
        content: str,
        memory_type: str = "fact",
        project_id: str | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Any: ...

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Any: ...

    async def rescope_memory(self, memory_id: str, new_project_id: str | None) -> Memory: ...

    async def sync_memory_scope_indices(
        self,
        memory_id: str,
        project_id: str | None,
    ) -> list[dict[str, str]]: ...

    async def delete_memory(self, memory_id: str) -> bool: ...

    async def reconcile_stores(self, dry_run: bool = False) -> dict[str, Any]: ...


class MemoryDreamLLMProtocol(Protocol):
    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        caller: str | None = None,
    ) -> dict[str, Any]: ...
