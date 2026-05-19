"""Dialect parity tests for the PostgreSQL hub migration."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _pytest.fixtures import FixtureLookupError

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _raw_fts_lines(source: str, pattern: str) -> list[str]:
    matcher = re.compile(pattern)
    return [line.strip() for line in source.splitlines() if matcher.search(line)]


def _hub_db(request: pytest.FixtureRequest) -> object:
    try:
        return request.getfixturevalue("hub_db")
    except FixtureLookupError:
        return request.getfixturevalue("temp_db")


class _VectorStoreStub:
    def __init__(self) -> None:
        self.hits: list[tuple[str, float]] = []

    async def search(
        self,
        _embedding: list[float],
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        return self.hits[:limit]

    async def upsert(
        self,
        _memory_id: str,
        _embedding: list[float],
        _payload: dict[str, Any],
    ) -> None:
        return None


class _KnowledgeGraphStub:
    def __init__(self, memory_ids: list[str]) -> None:
        self._memory_ids = memory_ids

    async def search_entities_by_vector(
        self,
        *,
        query_embedding: list[float],
        limit: int,
        min_score: float,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "entity_key": "topic:graph",
                "name": "graph",
                "entity_type": "topic",
                "labels": ["Topic"],
                "score": 1.0,
                "memory_ids": self._memory_ids[:limit],
            }
        ]

    async def find_related_memory_ids(
        self,
        *,
        entity_keys: list[str],
        max_hops: int,
        limit: int,
        project_id: str | None = None,
    ) -> list[str]:
        return []


async def _embed(_text: str, **_kwargs: object) -> list[float]:
    return [1.0, 0.0, 0.0]


def test_pick_search_backend_dispatches_by_hub_dialect_and_rejects_semantic() -> None:
    from gobby.storage.tasks._search import (
        BM25SearchBackend,
        FTS5SearchBackend,
        pick_search_backend,
    )

    sqlite_backend = pick_search_backend(SimpleNamespace(dialect="sqlite"), "tasks")
    postgres_backend = pick_search_backend(SimpleNamespace(dialect="postgres"), "tasks")

    assert isinstance(sqlite_backend, FTS5SearchBackend)
    assert isinstance(postgres_backend, BM25SearchBackend)
    with pytest.raises(NotImplementedError, match="Semantic search is a follow-up workstream"):
        pick_search_backend(SimpleNamespace(dialect="postgres"), "tasks", mode="semantic")


def test_memory_fts_search_uses_keyword_backend_seam() -> None:
    from gobby.memory.manager import MemoryManager

    assert hasattr(MemoryManager, "_fts_search")
    source = inspect.getsource(MemoryManager._fts_search)

    assert "pick_search_backend" in source
    assert '"memories"' in source or "'memories'" in source
    assert ".search(" in source
    assert "MemoryFTS5Searcher" not in source


def test_memory_fused_search_preserves_rrf_configuration_surface() -> None:
    from gobby.memory.manager import MemoryManager

    signature = inspect.signature(MemoryManager.__init__)

    assert "rrf_k" in signature.parameters
    assert "neo4j_rrf_k" in signature.parameters
    assert MemoryManager._rrf_scores(["keyword"], k=17)["keyword"] == pytest.approx(1 / 18)
    assert MemoryManager._rrf_scores(["graph"], k=29)["graph"] == pytest.approx(1 / 30)


@pytest.mark.parametrize(
    "signal_case",
    ["keyword_only", "vector_only", "graph_only", "combined_signal"],
)
async def test_fused_search_dialect_parity_cases(
    request: pytest.FixtureRequest, signal_case: str
) -> None:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.manager import MemoryManager

    hub_db = _hub_db(request)
    vector_store = _VectorStoreStub()
    manager = MemoryManager(
        db=hub_db,
        config=MemoryConfig(enabled=True, backend="local", access_debounce_seconds=0),
        vector_store=vector_store,
        embed_fn=_embed,
        rrf_k=17,
        neo4j_rrf_k=29,
    )

    keyword_strong = await manager.create_memory(content="alpha alpha phase four")
    keyword_medium = await manager.create_memory(content="alpha beta phase four")
    vector_only = await manager.create_memory(content="semantic vector target")
    graph_only = await manager.create_memory(content="graph target")
    shared = await manager.create_memory(content="alpha shared vector graph")

    query = "alpha"
    expected: list[str]
    if signal_case == "keyword_only":
        vector_store.hits = []
        expected = [keyword_strong.id, keyword_medium.id]
    elif signal_case == "vector_only":
        query = "semantic"
        vector_store.hits = [(vector_only.id, 0.99), (keyword_medium.id, 0.2)]
        expected = [vector_only.id, keyword_medium.id]
    elif signal_case == "graph_only":
        query = "graph"
        vector_store.hits = []
        graph = _KnowledgeGraphStub([graph_only.id])
        manager._search_service.kg_service = graph
        expected = [graph_only.id]
    else:
        vector_store.hits = [(vector_only.id, 0.9), (shared.id, 0.8)]
        graph = _KnowledgeGraphStub([shared.id])
        manager._search_service.kg_service = graph
        expected = [vector_only.id, shared.id]

    results = await manager.search_memories(query=query, limit=len(expected))

    assert [memory.id for memory in results] == expected


def test_skills_search_dialect_parity(request: pytest.FixtureRequest) -> None:
    from gobby.search import SearchConfig
    from gobby.skills.search import SkillSearch
    from gobby.storage.skills import Skill

    skills_search = _source("src/gobby/skills/search.py")
    skills_manager = _source("src/gobby/skills/manager.py")
    combined = f"{skills_search}\n{skills_manager}"

    if "HubDatabase" not in combined:
        pytest.fail("skills search must accept the HubDatabase seam")
    if "pick_search_backend" not in combined:
        pytest.fail("skills search must use the shared backend picker")

    raw_sql_lines = _raw_fts_lines(combined, r"MATCH|bm25\(")
    assert not raw_sql_lines
    assert "_is_sqlite" in skills_search

    search = SkillSearch(db=_hub_db(request), config=SearchConfig(mode="keyword"))
    search.index_skills(
        [
            Skill(
                id="skl-git-workflow",
                name="git-workflow",
                description="Git workflow branching merge",
                content="workflow workflow git",
            ),
            Skill(
                id="skl-commit",
                name="commit-message",
                description="Git commit message workflow",
                content="commit git",
            ),
            Skill(
                id="skl-review",
                name="code-review",
                description="Review implementation quality",
                content="review",
            ),
        ]
    )

    assert [result.skill_name for result in search.search("git workflow", top_k=2)] == [
        "git-workflow",
        "commit-message",
    ]


def test_code_search_dialect_parity(request: pytest.FixtureRequest) -> None:
    from gobby.code_index.models import ContentChunk, Symbol
    from gobby.code_index.storage import CodeIndexStorage

    code_storage = _source("src/gobby/code_index/storage.py")

    if "HubDatabase" not in code_storage:
        pytest.fail("code search must accept the HubDatabase seam")
    if "pick_search_backend" not in code_storage:
        pytest.fail("code search must use the shared backend picker")

    raw_fts_lines = _raw_fts_lines(code_storage, r"code_symbols_fts|code_content_fts|bm25\(")
    assert not raw_fts_lines

    storage = CodeIndexStorage(_hub_db(request))
    storage.upsert_symbols(
        [
            Symbol(
                id="sym-calculator",
                project_id="proj-1",
                file_path="src/calculator.py",
                name="Calculator",
                qualified_name="Calculator",
                kind="class",
                language="python",
                byte_start=0,
                byte_end=100,
                line_start=1,
                line_end=10,
                signature="class Calculator:",
                docstring="Alpha beta calculator",
                content_hash="hash-calculator",
            ),
            Symbol(
                id="sym-helper",
                project_id="proj-1",
                file_path="src/helper.py",
                name="Helper",
                qualified_name="Helper",
                kind="class",
                language="python",
                byte_start=0,
                byte_end=100,
                line_start=1,
                line_end=10,
                signature="class Helper:",
                docstring="Gamma helper",
                content_hash="hash-helper",
            ),
        ]
    )
    storage.upsert_content_chunks(
        [
            ContentChunk(
                id="chunk-calculator",
                project_id="proj-1",
                file_path="src/calculator.py",
                chunk_index=0,
                line_start=1,
                line_end=10,
                content="alpha alpha beta calculator",
                language="python",
            ),
            ContentChunk(
                id="chunk-helper",
                project_id="proj-1",
                file_path="src/helper.py",
                chunk_index=0,
                line_start=1,
                line_end=10,
                content="alpha gamma helper",
                language="python",
            ),
        ]
    )

    assert [symbol.name for symbol in storage.search_symbols_fts("Calculator", "proj-1")] == [
        "Calculator"
    ]
    assert [
        result["file_path"] for result in storage.search_content_fts("alpha beta", "proj-1")
    ] == ["src/calculator.py"]
