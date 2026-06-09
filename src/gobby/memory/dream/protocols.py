"""Shared protocols for memory dream collaborators."""

from __future__ import annotations

from typing import Any, Protocol

from gobby.storage.hub.protocol import HubDatabase


class MemoryDreamManagerProtocol(Protocol):
    db: HubDatabase

    async def alist_memories(self, *, limit: int | None, offset: int) -> list[Any]: ...

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
