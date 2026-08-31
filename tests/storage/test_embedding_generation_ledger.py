"""Ledger watermark ordering and same-transaction producer events."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.projects.purge import ProjectPurgeService
from gobby.storage.embedding_generation_state import (
    EmbeddingGenerationError,
    EmbeddingGenerationLeaseLost,
    EmbeddingGenerationState,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import LocalProjectManager

pytestmark = pytest.mark.integration


def _events(db: HubDatabase, source_id: str) -> list[tuple[str, bool]]:
    rows = db.fetchall(
        "SELECT source_kind, is_tombstone FROM embedding_projection_changes "
        "WHERE source_id = %s ORDER BY sequence",
        (source_id,),
    )
    return [(str(row["source_kind"]), bool(row["is_tombstone"])) for row in rows]


def test_watermark_waits_for_inflight_producers(temp_db: HubDatabase) -> None:
    state = EmbeddingGenerationState(temp_db)
    started = threading.Event()
    release = threading.Event()
    allocated: list[int] = []
    watermarks: list[int] = []
    reader_started = threading.Event()

    def producer() -> None:
        with temp_db.transaction() as conn:
            allocated.append(state.append_change("memory", "m-race", transaction=conn))
            started.set()
            assert release.wait(timeout=5)

    def reader() -> None:
        reader_started.set()
        watermarks.append(state.watermark())

    producer_thread = threading.Thread(target=producer)
    producer_thread.start()
    assert started.wait(timeout=5)
    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    assert reader_started.wait(timeout=5)
    # The exclusive watermark read must wait out the in-flight shared allocator.
    assert not watermarks
    release.set()
    producer_thread.join(timeout=5)
    reader_thread.join(timeout=5)

    assert allocated and watermarks
    assert watermarks[0] >= allocated[0]


def test_watermark_rejects_ambient_transaction(temp_db: HubDatabase) -> None:
    state = EmbeddingGenerationState(temp_db)

    with (
        temp_db.transaction(),
        pytest.raises(
            EmbeddingGenerationError,
            match="outside an ambient transaction",
        ),
    ):
        state.watermark()


def test_rolled_back_producer_leaves_no_ledger_event(temp_db: HubDatabase) -> None:
    state = EmbeddingGenerationState(temp_db)

    with pytest.raises(RuntimeError, match="boom"):
        with temp_db.transaction() as conn:
            state.append_change("memory", "m-rollback", transaction=conn)
            raise RuntimeError("boom")

    assert _events(temp_db, "m-rollback") == []


def test_dream_visibility_producers_append_events(temp_db: HubDatabase) -> None:
    project = LocalProjectManager(temp_db).create(name="dream-ledger")
    manager = LocalMemoryManager(temp_db)
    memory = manager.create_memory("dream ledger target", project.id)

    manager.mark_dreamed(memory.id, hidden_as="review")
    assert _events(temp_db, memory.id)[-1] == ("memory", True)

    manager.restore_memory(memory.id)
    assert _events(temp_db, memory.id)[-1] == ("memory", False)

    manager.mark_dreamed(memory.id, hidden_as="delete", when="2000-01-01T00:00:00+00:00")
    purged = manager.purge_dream_hidden("delete", 0)
    assert memory.id in purged
    assert _events(temp_db, memory.id)[-1] == ("memory", True)


def test_project_purge_tombstones_embedded_artifacts(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.projects.purge._LEDGER_PURGE_BATCH_SIZE", 1)
    project = LocalProjectManager(temp_db).create(name="purge-ledger")
    memory = LocalMemoryManager(temp_db).create_memory("purge ledger target", project.id)
    second_memory = LocalMemoryManager(temp_db).create_memory(
        "second purge ledger target", project.id
    )

    mcp_manager = LocalMCPManager(temp_db)
    server = mcp_manager.upsert(
        name="purge-ledger-server",
        transport="http",
        url="http://localhost:8080",
        project_id=project.id,
    )
    mcp_manager.cache_tools(
        server.id,
        [{"name": "purge_tool", "description": "doomed", "inputSchema": {}}],
    )
    tool_row = temp_db.fetchone(
        "SELECT tools.id AS id FROM tools JOIN mcp_servers "
        "ON mcp_servers.id = tools.mcp_server_id WHERE mcp_servers.project_id = %s",
        (project.id,),
    )
    assert tool_row is not None
    tool_id = str(tool_row["id"])

    host = cast(Any, SimpleNamespace(db=temp_db))
    ProjectPurgeService._delete_hub_rows(host, project.id)

    assert temp_db.fetchone("SELECT 1 FROM memories WHERE id = %s", (memory.id,)) is None
    assert _events(temp_db, memory.id)[-1] == ("memory", True)
    assert _events(temp_db, second_memory.id)[-1] == ("memory", True)
    assert _events(temp_db, tool_id)[-1] == ("tool", True)


def test_replace_tools_appends_tombstones_and_content_events(
    temp_db: HubDatabase,
) -> None:
    project = LocalProjectManager(temp_db).create(name="mcp-ledger")
    mcp_manager = LocalMCPManager(temp_db)
    server = mcp_manager.upsert(
        name="mcp-ledger-server",
        transport="http",
        url="http://localhost:8080",
        project_id=project.id,
    )
    mcp_manager.cache_tools(
        server.id,
        [{"name": "old_tool", "description": "stale", "inputSchema": {}}],
    )
    old_tools = mcp_manager.get_cached_tools(server.id)
    assert len(old_tools) == 1
    old_tool_id = old_tools[0].id

    with temp_db.transaction() as conn:
        mcp_manager._replace_tools_for_server_id(conn, server.id, old_tools)

    assert _events(temp_db, old_tool_id)[-1] == ("tool", True)
    replacements = mcp_manager.get_cached_tools(server.id)
    assert len(replacements) == 1
    assert replacements[0].id != old_tool_id
    assert _events(temp_db, replacements[0].id)[-1] == ("tool", False)


def test_expired_serving_ack_unblocks_collection(temp_db: HubDatabase) -> None:
    """A live ack blocks incompatible-generation GC until its DB lease lapses."""
    from uuid import uuid4

    state = EmbeddingGenerationState(temp_db)
    watermark = state.watermark()
    daemon_instance_id = uuid4()
    lease = state.prepare_serving_lease(
        daemon_instance_id,
        "old-generation",
        0,
        lease_seconds=0.2,
        caught_up_watermark=watermark,
        required_watermark=watermark,
    )
    lease.activate()

    if lease.remaining_seconds() <= 0:
        pytest.skip("200ms serving lease expired before live-ack assertion")
    assert state.can_collect("new-generation", 1) is False

    temp_db.execute(
        "UPDATE embedding_generation_acks "
        "SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
        "WHERE daemon_instance_id = %s",
        (daemon_instance_id,),
    )
    assert state.can_collect("new-generation", 1) is True


def test_renew_rejects_an_already_expired_database_lease(temp_db: HubDatabase) -> None:
    from uuid import uuid4

    state = EmbeddingGenerationState(temp_db)
    watermark = state.watermark()
    daemon_instance_id = uuid4()
    lease = state.prepare_serving_lease(
        daemon_instance_id,
        "expired-generation",
        1,
        lease_seconds=30,
        caught_up_watermark=watermark,
        required_watermark=watermark,
    )
    lease.activate()
    temp_db.execute(
        "UPDATE embedding_generation_acks "
        "SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
        "WHERE daemon_instance_id = %s",
        (daemon_instance_id,),
    )

    with pytest.raises(EmbeddingGenerationLeaseLost, match="no longer matches"):
        lease.renew()

    expired = temp_db.fetchone(
        "SELECT lease_expires_at <= CURRENT_TIMESTAMP AS expired "
        "FROM embedding_generation_acks WHERE daemon_instance_id = %s",
        (daemon_instance_id,),
    )
    assert expired is not None and expired["expired"] is True


def test_stale_activation_cannot_overwrite_successor_ack(temp_db: HubDatabase) -> None:
    from uuid import uuid4

    state = EmbeddingGenerationState(temp_db)
    daemon_instance_id = uuid4()
    successor = state.prepare_serving_lease(
        daemon_instance_id,
        "new-generation",
        9,
        lease_seconds=30,
        caught_up_watermark=0,
        required_watermark=0,
    )
    stale = state.prepare_serving_lease(
        daemon_instance_id,
        "old-generation",
        7,
        lease_seconds=30,
        caught_up_watermark=0,
        required_watermark=0,
    )
    successor.activate()

    with pytest.raises(EmbeddingGenerationLeaseLost, match="newer serving lease"):
        stale.activate()

    row = temp_db.fetchone(
        "SELECT generation, committed_revision FROM embedding_generation_acks "
        "WHERE daemon_instance_id = %s",
        (daemon_instance_id,),
    )
    assert row is not None
    assert row["generation"] == "new-generation"
    assert row["committed_revision"] == 9
