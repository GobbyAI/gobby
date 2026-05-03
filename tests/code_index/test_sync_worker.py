"""Tests for code_index.sync_worker external store sync."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.code_index.models import IndexedFile, IndexedProject, Symbol
from gobby.code_index.storage import CodeIndexStorage
from gobby.code_index.sync_worker import _sync_file
from gobby.config.code_index import CodeIndexConfig

pytestmark = pytest.mark.unit


class MissingCollectionError(Exception):
    """Raised by the fake vector store when a collection was not ensured."""


class FakeEmbedModel:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class RecoveringVectorStore:
    def __init__(self) -> None:
        self.collections: set[str] = set()
        self.calls: list[tuple[str, str, Any]] = []
        self.items: list[tuple[str, list[float], dict[str, Any]]] = []

    async def ensure_collection(
        self, collection_name: str, embedding_dim: int | None = None
    ) -> None:
        self.calls.append(("ensure_collection", collection_name, embedding_dim))
        self.collections.add(collection_name)

    async def delete(self, filters: dict[str, str], collection_name: str) -> None:
        self.calls.append(("delete", collection_name, filters))
        if collection_name not in self.collections:
            raise MissingCollectionError(collection_name)

    async def batch_upsert(
        self,
        items: list[tuple[str, list[float], dict[str, Any]]],
        collection_name: str,
    ) -> None:
        self.calls.append(("batch_upsert", collection_name, len(items)))
        if collection_name not in self.collections:
            raise MissingCollectionError(collection_name)
        self.items = items


@pytest.mark.asyncio
async def test_sync_file_ensures_missing_vector_collection_before_upsert(
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
    tmp_path: Path,
) -> None:
    """A missing project-specific Qdrant collection is recreated before vector upsert."""
    project_id = "proj-1"
    file_path = "src/app.py"
    root = tmp_path
    source_file = root / file_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def greet(name: str) -> str:\n    return name\n")

    indexed_file = IndexedFile(
        id=IndexedFile.make_id(project_id, file_path),
        project_id=project_id,
        file_path=file_path,
        language="python",
        content_hash="abc123",
        symbol_count=len(sample_symbols),
        graph_synced=1,
        vectors_synced=0,
    )
    code_storage.upsert_project_stats(
        IndexedProject(
            id=project_id,
            root_path=str(root),
            total_files=1,
            total_symbols=len(sample_symbols),
        )
    )
    code_storage.upsert_file(indexed_file)
    code_storage.upsert_symbols(sample_symbols)

    vector_store = RecoveringVectorStore()

    did_sync = await _sync_file(
        storage=code_storage,
        vector_store=vector_store,
        graph=None,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
        embed_model=FakeEmbedModel(),
        project_id=project_id,
        root=root,
        file=indexed_file,
        embedding_dim=4,
    )

    collection = f"code_symbols_{project_id}"
    assert did_sync is True
    assert [call[0] for call in vector_store.calls] == [
        "ensure_collection",
        "delete",
        "batch_upsert",
    ]
    assert vector_store.calls[0] == ("ensure_collection", collection, 4)
    assert len(vector_store.items) == len(sample_symbols)

    synced_file = code_storage.get_file(project_id, file_path)
    assert synced_file is not None
    assert synced_file.vectors_synced == 1
