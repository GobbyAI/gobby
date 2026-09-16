"""Collection naming conventions for embedding-backed stores."""

from __future__ import annotations

from typing import Protocol

EMBEDDING_COLLECTION_KINDS: tuple[str, ...] = (
    "memories",
    "tool_embeddings",
)

# Leftover Qdrant aliases and `kind@run` physicals to delete; not a managed kind.
RETIRED_EMBEDDING_COLLECTION_KINDS: tuple[str, ...] = ("gobby_github_issues",)


def is_retired_embedding_collection(name: str) -> bool:
    """Return whether ``name`` is a retired alias or physical collection."""
    return name.split("@", 1)[0] in RETIRED_EMBEDDING_COLLECTION_KINDS


class EmbeddingCollectionStore(Protocol):
    async def get_aliases(self) -> dict[str, str]: ...

    async def list_collection_names(self) -> list[str]: ...

    async def delete_alias(self, alias_name: str) -> None: ...

    async def delete_collection(self, collection_name: str) -> None: ...


def _is_missing_collection_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "not found" in message or "does not exist" in message or "doesn't exist" in message


async def delete_retired_embedding_collections(store: EmbeddingCollectionStore) -> int:
    """Drop leftover retired aliases and physical collections if they still exist."""
    aliases = await store.get_aliases()
    for alias in list(aliases):
        if not is_retired_embedding_collection(alias):
            continue
        try:
            await store.delete_alias(alias)
        except Exception as exc:
            if not _is_missing_collection_error(exc):
                raise
    deleted = 0
    for name in await store.list_collection_names():
        if not is_retired_embedding_collection(name):
            continue
        try:
            await store.delete_collection(name)
        except Exception as exc:
            if not _is_missing_collection_error(exc):
                raise
        deleted += 1
    return deleted


class CollectionNameResolver:
    """Resolve serving aliases and versioned physical collection names."""

    def __init__(self, kinds: tuple[str, ...] = EMBEDDING_COLLECTION_KINDS) -> None:
        self._kinds = kinds

    @property
    def kinds(self) -> tuple[str, ...]:
        return self._kinds

    def active_alias(self, kind: str) -> str:
        """Return the serving alias name for a collection kind."""
        return kind

    def physical_name(self, kind: str, run_id: str) -> str:
        """Return the versioned physical collection name for a build run."""
        return f"{kind}@{run_id}"

    def parse_physical_name(self, name: str) -> tuple[str, str] | None:
        """Parse a physical name into ``(kind, run_id)`` when applicable."""
        if "@" not in name:
            return None
        kind, run_id = name.split("@", 1)
        return kind, run_id

    def is_physical_name(self, name: str) -> bool:
        """Return whether a name is a versioned physical collection name."""
        return "@" in name

    def all_physical_names(self, run_id: str) -> list[str]:
        """Return physical names for all managed kinds for a build run."""
        return [self.physical_name(kind, run_id) for kind in self._kinds]

    def all_active_aliases(self) -> list[str]:
        """Return serving alias names for all managed kinds."""
        return [self.active_alias(kind) for kind in self._kinds]
