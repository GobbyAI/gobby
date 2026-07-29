"""E2E: memory dream GC soft-delete lifecycle against an isolated daemon.

Exercises the design plan's e2e verification matrix end to end:

1. A full dream sweep soft-hides obsolete memories (contradicting current
   truth) while keeping current ones.
2. Soft-hidden memories leave agent recall (``visibility=active``) yet remain
   observable at ``GET /api/memories?visibility=hidden`` with their
   ``dream_action``.
3. An immediate re-run is a no-op (the cooldown stamp drains the candidate set).
4. ``POST /api/memories/{id}/restore`` returns a hidden memory to active.
5. The grace purge hard-removes aged hidden rows.

The live daemon is the system under test for the HTTP visibility/restore
surface: seeded rows are read back, restored, and counted through its real
``/api/memories`` routes. The sweep and purge run in-process against the
*same* isolated Postgres schema using the real ``LocalMemoryManager`` and
dream service code — only the LLM planner is faked (the matrix mandates no
live LLM), so the real validator, apply, ``mark_dreamed``, snapshot, and purge
paths all execute.

Before daemon startup, the fixture resolves managed FalkorDB credentials from
the authoritative config and secret stores, verifies authentication, and binds
an isolated graph with teardown cleanup. Daemon health must report the graph
subsystem as healthy before the memory and dream/GC assertions run.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from falkordb.asyncio import FalkorDB

from gobby.cli.installers.compose_env import resolve_compose_runtime
from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.service import run_memory_dream
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.e2e

# Memories whose content contradicts the current platform truth (FalkorDB graph
# backend, Postgres hub) must be flagged for deletion; everything else is kept.
OBSOLETE_MARKERS = ("neo4j", "mysql")

NEO4J_CONTENT = "The Gobby knowledge graph is backed by Neo4j."
MYSQL_CONTENT = "Gobby stores all hub data in a MySQL database."
CURRENT_CONTENT = "Gobby is a local-first daemon that unifies AI coding tools."


@pytest.fixture
def e2e_pre_daemon_setup(
    e2e_config: tuple[Path, int, int],
    monkeypatch: pytest.MonkeyPatch,
    postgres_schema: str,
    request: pytest.FixtureRequest,
) -> None:
    """Require managed FalkorDB and configure an isolated authenticated graph."""
    config_path, _http_port, _ws_port = e2e_config
    monkeypatch.delenv("GOBBY_FALKORDB_PASSWORD", raising=False)
    try:
        runtime = resolve_compose_runtime(
            Path.home() / ".gobby",
            profiles=("falkordb",),
        )
        password = runtime.environment["GOBBY_FALKORDB_PASSWORD"]
        port = int(runtime.environment["GOBBY_FALKORDB_PORT"])
        asyncio.run(_verify_falkordb_prerequisite("127.0.0.1", port, password))
    except Exception as exc:
        pytest.fail(
            "Memory dream/GC E2E requires a complete managed local install with "
            f"authenticated FalkorDB: {exc}"
        )

    graph_identity = hashlib.sha256(postgres_schema.encode()).hexdigest()[:16]
    graph_name = f"gobby_memory_e2e_{graph_identity}"

    def cleanup_graph() -> None:
        asyncio.run(
            _delete_falkordb_graph(
                host="127.0.0.1",
                port=port,
                password=password,
                graph_name=graph_name,
            )
        )

    request.addfinalizer(cleanup_graph)
    monkeypatch.setenv("GOBBY_FALKORDB_PASSWORD", password)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\ndatabases:\n"
            '  qdrant:\n    url: "http://127.0.0.1:6333"\n'
            "  falkordb:\n"
            '    host: "127.0.0.1"\n'
            f"    port: {port}\n"
            '    password: "${GOBBY_FALKORDB_PASSWORD}"\n'
            f'    graph_name: "{graph_name}"\n'
        )


async def _verify_falkordb_prerequisite(host: str, port: int, password: str) -> None:
    client = FalkorDB(host=host, port=port, password=password)
    try:
        await client.list_graphs()
    finally:
        await client.aclose()


async def _delete_falkordb_graph(
    *,
    host: str,
    port: int,
    password: str,
    graph_name: str,
) -> None:
    client = FalkorDB(host=host, port=port, password=password)
    try:
        if graph_name in await client.list_graphs():
            await client.select_graph(graph_name).delete()
    finally:
        await client.aclose()


def _sweep_config() -> MemoryDreamConfig:
    """Real dream config with secondary-store reconcile disabled.

    The storage ``LocalMemoryManager`` implements the candidate query, stamping,
    and snapshot subset the keep/delete sweep needs. Reconcile and vector-backed
    related evidence stay off. All remaining defaults suit the test as-is.
    """
    return MemoryDreamConfig(
        reconcile_after_apply=False,
        reconcile_after_revert=False,
        related_evidence_enabled=False,
    )


def _content_driven_planner(**kwargs: Any) -> dict[str, Any]:
    """Fake planner: delete obsolete-marker memories, keep the rest.

    Patched in for ``gobby.memory.dream.orchestrator.build_raw_plan`` so the
    sweep is deterministic without a live LLM while the real validate/apply
    path runs.
    """
    candidates = kwargs["candidates"]
    actions: list[dict[str, Any]] = []
    for candidate in candidates:
        text = candidate.content.lower()
        if any(marker in text for marker in OBSOLETE_MARKERS):
            actions.append(
                {
                    "action": "delete",
                    "memory_id": candidate.id,
                    "confidence": 1.0,
                    "reason": "contradicts current platform truth",
                }
            )
        else:
            actions.append(
                {
                    "action": "keep",
                    "memory_id": candidate.id,
                    "confidence": 1.0,
                    "reason": "still true and useful now",
                }
            )
    return {"actions": actions, "planner_errors": []}


async def _list_by_visibility(
    client: httpx.AsyncClient, visibility: str
) -> tuple[set[str], dict[str, Any]]:
    """Return (set of contents, {content: row}) for a daemon HTTP visibility scope."""
    response = await client.get("/api/memories", params={"visibility": visibility})
    assert response.status_code == 200, response.text
    payload = response.json()
    rows = {row["content"]: row for row in payload["memories"]}
    # Total must track the filtered page, not the unfiltered table.
    assert payload["total_memories"] == len(rows), payload
    return set(rows), rows


async def _run_sweep(manager: LocalMemoryManager) -> dict[str, Any]:
    with patch(
        "gobby.memory.dream.orchestrator.build_raw_plan",
        AsyncMock(side_effect=_content_driven_planner),
    ):
        # The storage manager covers the protocol subset this keep/delete sweep
        # exercises (candidates, mark_dreamed, snapshot store via .db); the
        # async consolidation/reconcile members are never reached here.
        # These memories are global (is_global=True on the personal project); a
        # sweep must carry an explicit scope, so target the global bucket.
        # Ordinary work units require a planner; the MagicMock stands in for
        # the LLM service while the patched build_raw_plan supplies the plan.
        return await run_memory_dream(
            memory_manager=cast(MemoryDreamManagerProtocol, manager),
            dream_config=_sweep_config(),
            llm_service=cast(Any, MagicMock()),
            global_only=True,
        )


@pytest.mark.asyncio
async def test_dream_gc_soft_delete_lifecycle(
    async_daemon_client: httpx.AsyncClient,
    postgres_db: HubDatabase,
) -> None:
    status_response = await async_daemon_client.get("/api/admin/status")
    assert status_response.status_code == 200, status_response.text
    assert "memory_knowledge_graph" not in status_response.json()["degraded_services"]

    # The test manager and the live daemon share the isolated worker schema, so
    # rows written here are served verbatim by the daemon's HTTP routes.
    manager = LocalMemoryManager(postgres_db)
    cast(Any, manager).notify_memory_changed = manager.notify_changed
    neo4j = manager.create_memory(
        content=NEO4J_CONTENT,
        project_id=PERSONAL_PROJECT_ID,
        memory_type="fact",
        is_global=True,
    )
    mysql = manager.create_memory(
        content=MYSQL_CONTENT,
        project_id=PERSONAL_PROJECT_ID,
        memory_type="fact",
        is_global=True,
    )
    manager.create_memory(
        content=CURRENT_CONTENT,
        project_id=PERSONAL_PROJECT_ID,
        memory_type="fact",
        is_global=True,
    )

    # All three are visible to agent recall before the sweep.
    active, _ = await _list_by_visibility(async_daemon_client, "active")
    assert active == {NEO4J_CONTENT, MYSQL_CONTENT, CURRENT_CONTENT}

    # 1. Full sweep: obsolete memories are soft-hidden, the current one kept.
    result = await _run_sweep(manager)
    assert result["success"] is True, result
    summary = result["run"]["summary"]
    assert summary["candidates_reviewed"] == 3
    assert summary["mutations"] == 2
    assert summary["actions"].get("delete") == 2
    assert summary["actions"].get("keep") == 1

    # 2. Hidden memories leave recall but remain observable at visibility=hidden.
    active, _ = await _list_by_visibility(async_daemon_client, "active")
    assert active == {CURRENT_CONTENT}

    hidden, hidden_rows = await _list_by_visibility(async_daemon_client, "hidden")
    assert hidden == {NEO4J_CONTENT, MYSQL_CONTENT}
    assert hidden_rows[NEO4J_CONTENT]["dream_action"] == "delete"
    assert hidden_rows[MYSQL_CONTENT]["dream_action"] == "delete"
    assert hidden_rows[NEO4J_CONTENT]["deleted_at"] is not None
    assert hidden_rows[MYSQL_CONTENT]["deleted_at"] is not None

    all_scope, _ = await _list_by_visibility(async_daemon_client, "all")
    assert all_scope == {NEO4J_CONTENT, MYSQL_CONTENT, CURRENT_CONTENT}

    # 3. Immediate re-run is a no-op: every row was just stamped inside the
    #    redream cooldown window, so the candidate set drains to empty.
    rerun = await _run_sweep(manager)
    rerun_summary = rerun["run"]["summary"]
    assert rerun_summary["candidates_reviewed"] == 0
    assert rerun_summary["pages"] == 0
    # Nothing changed: still exactly one active, two hidden.
    active, _ = await _list_by_visibility(async_daemon_client, "active")
    assert active == {CURRENT_CONTENT}

    # 4. Restore brings a hidden memory back to active via the daemon HTTP route.
    restore_resp = await async_daemon_client.post(f"/api/memories/{neo4j.id}/restore")
    assert restore_resp.status_code == 200, restore_resp.text
    restored = restore_resp.json()
    assert restored["deleted_at"] is None
    assert restored["dream_action"] is None

    active, _ = await _list_by_visibility(async_daemon_client, "active")
    assert active == {CURRENT_CONTENT, NEO4J_CONTENT}
    hidden, _ = await _list_by_visibility(async_daemon_client, "hidden")
    assert hidden == {MYSQL_CONTENT}

    # 5. Purge hard-removes aged hidden rows. Age the surviving hidden row past
    #    the delete grace window, then purge that action class.
    aged_when = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    manager.mark_dreamed(mysql.id, hidden_as="delete", when=aged_when)
    purged = manager.purge_dream_hidden("delete", older_than_days=30)
    assert mysql.id in purged

    # The purged row is gone from every visibility scope; the restored and the
    # always-current memory remain.
    all_scope, _ = await _list_by_visibility(async_daemon_client, "all")
    assert all_scope == {CURRENT_CONTENT, NEO4J_CONTENT}
    hidden, _ = await _list_by_visibility(async_daemon_client, "hidden")
    assert hidden == set()
