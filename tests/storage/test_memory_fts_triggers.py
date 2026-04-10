"""Regression tests for memory FTS5 trigger scoping and rebuild.

Verifies that the memories_fts_au trigger only fires on indexed column
updates (content, tags, memory_type, source_type) and NOT on bookkeeping
columns (access_count, last_accessed_at, graph_processed, etc.).

The key technique: drop the FTS virtual table to simulate corruption,
then verify that non-indexed column updates succeed (trigger doesn't fire)
while indexed column updates fail (trigger fires into a missing table).

See: migration 206 — _narrow_memories_fts_update_trigger
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    database = LocalDatabase(tmp_path / "gobby-hub.db")
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def memory_manager(db):
    return LocalMemoryManager(db)


def _fts_match(db: LocalDatabase, query: str) -> list[str]:
    """Return memory IDs that match the given FTS query."""
    rows = db.fetchall(
        """
        SELECT m.id FROM memories_fts f
        JOIN memories m ON m.rowid = f.rowid
        WHERE memories_fts MATCH ?
        """,
        (query,),
    )
    return [row["id"] for row in rows]


def _create_test_memory(memory_manager: LocalMemoryManager, suffix: str = "") -> str:
    """Create a memory and return its ID."""
    mem = memory_manager.create_memory(
        content=f"FTS trigger test memory {suffix}",
        memory_type="fact",
        tags=["fts", "test"],
    )
    return mem.id


def _drop_fts_table(db: LocalDatabase) -> None:
    """Drop the FTS virtual table (simulates corruption).

    After this, any trigger that tries to INSERT INTO memories_fts will
    raise an OperationalError, proving the trigger fired.
    """
    db.connection.executescript("""
        DROP TRIGGER IF EXISTS memories_fts_ai;
        DROP TRIGGER IF EXISTS memories_fts_ad;
        DROP TRIGGER IF EXISTS memories_fts_au;
        DROP TABLE IF EXISTS memories_fts;
    """)


def _drop_fts_table_keep_triggers(db: LocalDatabase) -> None:
    """Drop only the FTS virtual table, keep triggers intact.

    This means the triggers still exist and will fire, but their
    INSERT INTO memories_fts statements will fail because the target
    table is gone. This lets us distinguish trigger-fires from
    trigger-does-not-fire.
    """
    db.connection.execute("DROP TABLE IF EXISTS memories_fts")


class TestFTSTriggerScoping:
    """Verify the update trigger only fires on indexed columns."""

    def test_access_stats_update_succeeds_without_fts(
        self, db, memory_manager
    ) -> None:
        """access_count/last_accessed_at updates must not invoke the FTS trigger.

        With the FTS table dropped but triggers retained, a non-indexed
        column update must succeed (trigger doesn't fire), proving it is
        scoped to indexed columns only.
        """
        mem_id = _create_test_memory(memory_manager, "access")

        # Drop the FTS table but keep triggers
        _drop_fts_table_keep_triggers(db)

        # This must succeed — the update trigger should NOT fire
        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed_at = ?
                WHERE id = ?
                """,
                (datetime.now(UTC).isoformat(), mem_id),
            )

    def test_mark_graph_processed_succeeds_without_fts(
        self, db, memory_manager
    ) -> None:
        """graph_processed updates must not invoke the FTS trigger."""
        mem_id = _create_test_memory(memory_manager, "graphproc")

        _drop_fts_table_keep_triggers(db)

        # Must succeed — trigger should NOT fire on graph_processed
        memory_manager.mark_graph_processed(mem_id)

    def test_updated_at_succeeds_without_fts(self, db, memory_manager) -> None:
        """updated_at changes must not invoke the FTS trigger."""
        mem_id = _create_test_memory(memory_manager, "updatedat")

        _drop_fts_table_keep_triggers(db)

        with db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), mem_id),
            )

    def test_content_update_fires_trigger(self, db, memory_manager) -> None:
        """Updating content (an indexed column) MUST fire the FTS trigger.

        With FTS table dropped but triggers retained, a content update
        should fail because the trigger tries to INSERT INTO memories_fts.
        """
        mem_id = _create_test_memory(memory_manager, "contentfire")

        _drop_fts_table_keep_triggers(db)

        with pytest.raises(Exception, match="memories_fts"):
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE memories SET content = ? WHERE id = ?",
                    ("changed content", mem_id),
                )

    def test_content_update_refreshes_fts_row(self, db, memory_manager) -> None:
        """With healthy FTS, updating content must refresh the index."""
        mem_id = _create_test_memory(memory_manager, "xylophone")

        # Original content searchable
        assert mem_id in _fts_match(db, "xylophone")

        # Update content
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET content = ? WHERE id = ?",
                ("revised content kazoo", mem_id),
            )

        # Old term gone, new term searchable
        assert mem_id not in _fts_match(db, "xylophone")
        assert mem_id in _fts_match(db, "kazoo")

    def test_tags_update_refreshes_fts_row(self, db, memory_manager) -> None:
        """With healthy FTS, updating tags must refresh the index."""
        mem = memory_manager.create_memory(
            content="Tags scoping test",
            memory_type="fact",
            tags=["alpha"],
        )

        # Update tags
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET tags = ? WHERE id = ?",
                ('["beta", "gamma"]', mem.id),
            )

        # FTS should reflect new tags
        rows = db.fetchall(
            """
            SELECT f.tags FROM memories_fts f
            JOIN memories m ON m.rowid = f.rowid
            WHERE m.id = ?
            """,
            (mem.id,),
        )
        assert len(rows) == 1
        assert "beta" in rows[0]["tags"]
        assert "gamma" in rows[0]["tags"]


class TestFTSRebuild:
    """Verify FTS rebuild restores queryability."""

    def test_rebuild_restores_searchability(self, db, memory_manager) -> None:
        """After dropping and recreating FTS, rebuild restores search."""
        mem_id = _create_test_memory(memory_manager, "rebuildable")

        assert mem_id in _fts_match(db, "rebuildable")

        # Nuke and recreate FTS (clean state, no index data)
        _drop_fts_table(db)
        db.connection.executescript("""
            CREATE VIRTUAL TABLE memories_fts USING fts5(
                content, tags, memory_type, source_type,
                content='memories', content_rowid='rowid'
            );
        """)

        # Not searchable yet (index is empty)
        assert _fts_match(db, "rebuildable") == []

        # Rebuild
        db.connection.execute("""
            INSERT INTO memories_fts(rowid, content, tags, memory_type, source_type)
            SELECT rowid, content,
                   REPLACE(REPLACE(REPLACE(COALESCE(tags, ''), '"', ''), '[', ''), ']', ''),
                   memory_type, COALESCE(source_type, '')
            FROM memories
        """)

        # Now searchable again
        assert mem_id in _fts_match(db, "rebuildable")

    def test_rebuild_row_parity(self, db, memory_manager) -> None:
        """Rebuild must index every row in memories."""
        for i in range(5):
            _create_test_memory(memory_manager, f"parity{i}")

        mem_count = db.fetchone("SELECT count(*) as cnt FROM memories")["cnt"]

        # Nuke and recreate
        _drop_fts_table(db)
        db.connection.executescript("""
            CREATE VIRTUAL TABLE memories_fts USING fts5(
                content, tags, memory_type, source_type,
                content='memories', content_rowid='rowid'
            );
        """)

        # Rebuild
        db.connection.execute("""
            INSERT INTO memories_fts(rowid, content, tags, memory_type, source_type)
            SELECT rowid, content,
                   REPLACE(REPLACE(REPLACE(COALESCE(tags, ''), '"', ''), '[', ''), ']', ''),
                   memory_type, COALESCE(source_type, '')
            FROM memories
        """)

        # Use a broad query to check all rows are indexed
        indexed = db.fetchone(
            "SELECT count(*) as cnt FROM memories_fts WHERE memories_fts MATCH 'trigger OR test OR memory'"
        )["cnt"]
        assert indexed == mem_count


class TestFTSSearcherReindex:
    """Verify MemoryFTS5Searcher.reindex works correctly."""

    def test_reindex_method_restores_search(self, db, memory_manager) -> None:
        """MemoryFTS5Searcher.reindex must restore FTS from memories."""
        from gobby.memory.fts_search import MemoryFTS5Searcher

        mem_id = _create_test_memory(memory_manager, "reindexable")
        searcher = MemoryFTS5Searcher(db)

        # Verify initially searchable
        results = searcher.search("reindexable")
        assert any(r[0] == mem_id for r in results)

        # Reindex (clears and repopulates)
        result = searcher.reindex()
        assert result["success"] is True

        mem_count = db.fetchone("SELECT count(*) as cnt FROM memories")["cnt"]
        assert result["indexed"] == mem_count

        # Still searchable after reindex
        results = searcher.search("reindexable")
        assert any(r[0] == mem_id for r in results)
