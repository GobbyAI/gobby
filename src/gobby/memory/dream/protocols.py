"""Shared protocols for memory dream collaborators."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import Memory
from gobby.storage.memories_scope import MemoryScope


class MemoryDreamManagerProtocol(Protocol):
    db: HubDatabase

    async def alist_memories(self, *, limit: int | None, offset: int) -> list[Any]: ...

    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        scope: MemoryScope,
        memory_type: str | None = None,
    ) -> list[Any]: ...

    def list_dream_candidate_ids(
        self,
        *,
        redream_cutoff: str,
        scope: MemoryScope,
        memory_type: str | None = None,
        limit: int | None = None,
    ) -> list[str]: ...

    def get_memories(self, memory_ids: list[str], scope: MemoryScope) -> list[Any]: ...

    def list_dream_scopes(self, *, redream_cutoff: str) -> list[MemoryScope]: ...

    def mark_project_memories_due(self, project_id: str) -> int: ...

    def mark_global_memories_due(self) -> int: ...

    def notify_memory_changed(self) -> None: ...

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
        *,
        is_global: bool = False,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
    ) -> Any: ...

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Any: ...

    async def move_memory(self, memory_id: str, new_project_id: str) -> Memory: ...

    async def promote_memory(self, memory_id: str) -> Memory: ...

    async def demote_memory(self, memory_id: str) -> Memory: ...

    async def sync_memory_scope_indices(
        self,
        memory: Memory,
        *,
        previous_project_id: str | None = None,
        previous_is_global: bool | None = None,
        notify_changed: bool = True,
    ) -> list[dict[str, str]]: ...

    async def restore_memory_indices(
        self,
        memory_id: str,
        content: str,
        project_id: str,
        is_global: bool,
        memory_type: str,
        *,
        notify_changed: bool = True,
    ) -> bool: ...

    async def delete_memory(self, memory_id: str) -> bool: ...

    async def reconcile_stores(self, dry_run: bool = False) -> dict[str, Any]: ...


class MemoryDreamLLMProtocol(Protocol):
    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        json_schema: dict[str, Any],
        caller: str | None = None,
        total_timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...
