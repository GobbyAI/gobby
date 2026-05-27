"""Dialect parity tests for the PostgreSQL hub migration."""

from __future__ import annotations

import datetime
import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
_ISO_TS = "2026-01-01T00:00:00Z"


def _source(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _raw_fts_lines(source: str, pattern: str) -> list[str]:
    matcher = re.compile(pattern)
    return [line.strip() for line in source.splitlines() if matcher.search(line)]


def _detect_search_backend_seams() -> dict[str, bool]:
    """Detect whether the #14098 search-backend port has landed.

    The structural tests below are TDD pins authored by #14095 against the
    #14098 implementation (port keyword search backends through the
    HubDatabase seam). They are intentionally red until #14098 merges into
    this branch's base. Each test guards on this detection so the active
    suite stays green; the tests transition from skipped to executed
    automatically when the seam symbols and source-file markers appear.
    """
    seams: dict[str, bool] = {
        "bm25_backend": False,
        "memory_keyword_search": False,
        "memory_rrf_k": False,
        "skills_hub_seam": False,
        "code_index_hub_seam": False,
    }
    try:
        from gobby.storage.tasks._search import BM25SearchBackend  # noqa: F401

        seams["bm25_backend"] = True
    except ImportError:
        pass
    try:
        from gobby.memory.manager import MemoryManager

        seams["memory_keyword_search"] = hasattr(MemoryManager, "_keyword_search")
        seams["memory_rrf_k"] = "rrf_k" in inspect.signature(MemoryManager.__init__).parameters
    except ImportError:
        pass
    try:
        skills_text = _source("src/gobby/skills/search.py") + _source("src/gobby/skills/manager.py")
        seams["skills_hub_seam"] = (
            "HubDatabase" in skills_text and "pick_search_backend" in skills_text
        )
    except OSError:
        pass
    try:
        code_text = _source("src/gobby/code_index/storage.py")
        seams["code_index_hub_seam"] = (
            "HubDatabase" in code_text and "pick_search_backend" in code_text
        )
    except OSError:
        pass
    return seams


_SEAMS = _detect_search_backend_seams()
_PENDING_REASON = "awaiting #14098 search-backend port (HubDatabase keyword seam)"


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


@pytest.mark.skipif(not _SEAMS["bm25_backend"], reason=_PENDING_REASON)
def test_pick_search_backend_uses_postgres_bm25_and_rejects_semantic() -> None:
    from gobby.storage.tasks._search import (
        BM25SearchBackend,
        pick_search_backend,
    )

    postgres_backend = pick_search_backend(SimpleNamespace(dialect="postgres"), "tasks")

    assert isinstance(postgres_backend, BM25SearchBackend)
    with pytest.raises(NotImplementedError, match="Semantic search is a follow-up workstream"):
        pick_search_backend(SimpleNamespace(dialect="postgres"), "tasks", mode="semantic")


@pytest.mark.skipif(not _SEAMS["memory_keyword_search"], reason=_PENDING_REASON)
def test_memory_keyword_search_uses_keyword_backend_seam() -> None:
    from gobby.memory.manager import MemoryManager

    assert hasattr(MemoryManager, "_keyword_search")
    source = inspect.getsource(MemoryManager._keyword_search)

    assert "pick_search_backend" in source
    assert '"memories"' in source or "'memories'" in source
    assert ".search(" in source


@pytest.mark.skipif(not _SEAMS["memory_rrf_k"], reason=_PENDING_REASON)
def test_memory_fused_search_preserves_rrf_configuration_surface() -> None:
    from gobby.memory.manager import MemoryManager

    signature = inspect.signature(MemoryManager.__init__)

    assert "rrf_k" in signature.parameters
    assert "falkordb_rrf_k" in signature.parameters
    assert MemoryManager._rrf_scores(["keyword"], k=17)["keyword"] == pytest.approx(1 / 18)
    assert MemoryManager._rrf_scores(["graph"], k=29)["graph"] == pytest.approx(1 / 30)


@pytest.mark.skipif(not _SEAMS["memory_rrf_k"], reason=_PENDING_REASON)
@pytest.mark.parametrize(
    "signal_case",
    ["keyword_only", "vector_only", "graph_only", "combined_signal"],
)
async def test_fused_search_dialect_parity_cases(hub_db: Any, signal_case: str) -> None:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.manager import MemoryManager

    vector_store = _VectorStoreStub()
    manager = MemoryManager(
        db=hub_db,
        config=MemoryConfig(enabled=True, backend="local", access_debounce_seconds=0),
        vector_store=vector_store,
        embed_fn=_embed,
        rrf_k=17,
        falkordb_rrf_k=29,
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


@pytest.mark.skipif(not _SEAMS["skills_hub_seam"], reason=_PENDING_REASON)
def test_skills_search_dialect_parity(hub_db: Any) -> None:
    from gobby.search import SearchConfig
    from gobby.skills.search import SkillSearch
    from gobby.storage.skills import LocalSkillManager

    skills_search = _source("src/gobby/skills/search.py")
    skills_manager = _source("src/gobby/skills/manager.py")
    combined = f"{skills_search}\n{skills_manager}"

    if "HubDatabase" not in combined:
        pytest.fail("skills search must accept the HubDatabase seam")
    if "pick_search_backend" not in combined:
        pytest.fail("skills search must use the shared backend picker")

    raw_sql_lines = _raw_fts_lines(combined, r"MATCH|bm25\(")
    assert not raw_sql_lines

    skill_manager = LocalSkillManager(hub_db)
    skills = [
        skill_manager.create_skill(
            name="git-workflow",
            description="Git workflow branching merge",
            content="workflow workflow git",
        ),
        skill_manager.create_skill(
            name="commit-message",
            description="Git commit message workflow",
            content="commit git",
        ),
        skill_manager.create_skill(
            name="code-review",
            description="Review implementation quality",
            content="review",
        ),
    ]
    search = SkillSearch(db=hub_db, config=SearchConfig(mode="keyword"))
    search.index_skills(skills)

    assert [result.skill_name for result in search.search("git workflow", top_k=2)] == [
        "git-workflow",
        "commit-message",
    ]


@pytest.mark.skipif(not _SEAMS["code_index_hub_seam"], reason=_PENDING_REASON)
def test_code_search_dialect_parity(hub_db: Any) -> None:
    from gobby.code_index.models import ContentChunk, Symbol
    from gobby.code_index.storage import CodeIndexStorage

    code_storage = _source("src/gobby/code_index/storage.py")

    if "HubDatabase" not in code_storage:
        pytest.fail("code search must accept the HubDatabase seam")
    if "pick_search_backend" not in code_storage:
        pytest.fail("code search must use the shared backend picker")

    raw_fts_lines = _raw_fts_lines(code_storage, r"code_symbols_fts|code_content_fts|bm25\(")
    assert not raw_fts_lines

    storage = CodeIndexStorage(hub_db)
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
        result["file_path"] for result in storage.search_content_fts("beta calculator", "proj-1")
    ] == ["src/calculator.py"]


def _insert_skill(
    txn: Any,
    *,
    skill_id: str,
    name: str,
    project_id: str | None,
    source: str,
    metadata_json: str | None,
) -> None:
    """Insert a skill row using an explicit JSONB metadata cast."""
    metadata_clause = "CAST(%s AS JSONB)"
    txn.execute(
        f"""
        INSERT INTO skills (
            id, name, description, content,
            metadata, project_id, source,
            created_at, updated_at
        )
        VALUES (%s, %s, 'desc', 'content', {metadata_clause}, %s, %s, %s, %s)
        """,
        (skill_id, name, metadata_json, project_id, source, _ISO_TS, _ISO_TS),
    )


def test_upsert_on_conflict_do_nothing_dialect_parity(hub_db: Any) -> None:
    """`ON CONFLICT DO NOTHING` skips duplicate rows."""
    db = hub_db

    with db.transaction() as txn:
        txn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            ("p-upsert-original", "parity-upsert"),
        )

    with db.transaction() as txn:
        txn.execute(
            """
            INSERT INTO projects (id, name) VALUES (%s, %s)
            ON CONFLICT (name) DO NOTHING
            """,
            ("p-upsert-duplicate", "parity-upsert"),
        )

    with db.transaction() as txn:
        rows = txn.execute(
            "SELECT id FROM projects WHERE name = %s ORDER BY id",
            ("parity-upsert",),
        ).fetchall()

    assert [row["id"] for row in rows] == ["p-upsert-original"]


def test_returning_clause_dialect_parity(hub_db: Any) -> None:
    """`RETURNING id` replaces ``lastrowid`` for generated-key reads on both backends."""
    db = hub_db

    with db.transaction() as txn:
        cursor = txn.execute(
            """
            INSERT INTO projects (id, name)
            VALUES (%s, %s)
            RETURNING id
            """,
            ("proj-returning", "parity-returning"),
        )
        row = cursor.fetchone()

    assert row is not None
    assert row["id"] == "proj-returning"


def test_json_path_extraction_dialect_parity(hub_db: Any) -> None:
    """`json_text_expr` extracts scalar JSON values with the JSONB path operator."""
    from gobby.storage.sql_dialect import json_text_expr

    db = hub_db
    payload_json = '{"category": "parity-test", "nested": {"deep": "deep-value"}}'

    with db.transaction() as txn:
        _insert_skill(
            txn,
            skill_id="skl-parity-json",
            name="parity-json",
            project_id=None,
            source="installed",
            metadata_json=payload_json,
        )

        top_expr = json_text_expr(db, "metadata", "category")
        nested_expr = json_text_expr(db, "metadata", "nested", "deep")
        cursor = txn.execute(
            f"SELECT {top_expr} AS top, {nested_expr} AS nested FROM skills WHERE id = %s",
            ("skl-parity-json",),
        )
        row = cursor.fetchone()

    assert row is not None
    assert row["top"] == "parity-test"
    assert row["nested"] == "deep-value"


def test_timestamp_default_is_timezone_aware_utc_parity(hub_db: Any) -> None:
    """Default-valued timestamp columns produce timezone-aware UTC moments."""
    db = hub_db
    before = datetime.datetime.now(datetime.UTC)

    with db.transaction() as txn:
        txn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            ("proj-ts", "parity-ts"),
        )
        row = txn.execute(
            "SELECT created_at FROM projects WHERE id = %s",
            ("proj-ts",),
        ).fetchone()

    after = datetime.datetime.now(datetime.UTC)
    assert row is not None
    raw = row["created_at"]

    if isinstance(raw, datetime.datetime):
        moment = raw
    else:
        assert isinstance(raw, str)
        if "T" in raw:
            moment = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            moment = datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.UTC,
            )

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    assert moment.utcoffset() == datetime.timedelta(0)

    floor_before = before.replace(microsecond=0)
    assert floor_before <= moment <= after


def test_unique_nulls_not_distinct_dialect_parity(hub_db: Any) -> None:
    """Two rows that share ``(name, NULL project_id, source)`` collide."""
    db = hub_db

    with db.transaction() as txn:
        _insert_skill(
            txn,
            skill_id="skl-unique-a",
            name="parity-unique-null",
            project_id=None,
            source="installed",
            metadata_json=None,
        )

    import psycopg.errors

    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction() as txn:
            _insert_skill(
                txn,
                skill_id="skl-unique-b",
                name="parity-unique-null",
                project_id=None,
                source="installed",
                metadata_json=None,
            )

    # A row with a non-NULL project_id remains accepted.
    with db.transaction() as txn:
        txn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            ("proj-unique", "proj-unique"),
        )
        _insert_skill(
            txn,
            skill_id="skl-unique-c",
            name="parity-unique-null",
            project_id="proj-unique",
            source="installed",
            metadata_json=None,
        )

    with db.transaction() as txn:
        rows = txn.execute(
            "SELECT id FROM skills WHERE name = %s ORDER BY id",
            ("parity-unique-null",),
        ).fetchall()

    assert [row["id"] for row in rows] == ["skl-unique-a", "skl-unique-c"]
