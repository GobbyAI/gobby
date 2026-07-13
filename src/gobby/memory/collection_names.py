"""Collection naming conventions for embedding-backed stores."""

from __future__ import annotations

EMBEDDING_COLLECTION_KINDS: tuple[str, ...] = (
    "memories",
    "tool_embeddings",
    "gobby_github_issues",
)


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
