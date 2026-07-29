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

The daemon only builds its memory manager when embedding configuration is
present (``_init_memory_stack`` validates it), so this module injects a dummy
local embedding ``api_base`` before the daemon starts. It is a pure config
check that is never probed — the DB-backed list/restore/count routes never
touch embeddings or Qdrant.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

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
def e2e_pre_daemon_setup(e2e_config: tuple[Any, int, int], monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the daemon's memory manager by configuring an embedding api_base.

    Overrides the conftest no-op hook (runs after DB reset, before daemon
    start). Without an embedding config the daemon's ``_init_memory_stack``
    raises and leaves ``memory_manager`` unset, so the ``/api/memories`` routes
    return 500. The base is never contacted — it only satisfies the config
    presence check guarding memory-manager construction.
    """
    from gobby.config.bootstrap import load_bootstrap

    config_path, _http_port, _ws_port = e2e_config
    monkeypatch.setenv("GOBBY_FALKORDB_PASSWORD", load_bootstrap().falkordb_password)
    with open(config_path, "a") as handle:
        handle.write(
            '\nembeddings:\n  api_base: "http://127.0.0.1:9/v1"\n'
            'databases:\n  qdrant:\n    url: "http://127.0.0.1:6333"\n'
            '  falkordb:\n    password: "${GOBBY_FALKORDB_PASSWORD}"\n'
        )


def _sweep_config() -> MemoryDreamConfig:
    """Real dream config with secondary-store reconcile disabled.

    The storage ``LocalMemoryManager`` implements the candidate query, stamping,
    and snapshot subset the keep/delete sweep needs; it has no ``reconcile_stores``,
    so reconcile must stay off. All other defaults (planner_batch_size=25,
    redream_after_hours=20, purge_delete_after_days=30) suit the test as-is.
    """
    return MemoryDreamConfig(reconcile_after_apply=False, reconcile_after_revert=False)


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
    # The test manager and the live daemon share the isolated worker schema, so
    # rows written here are served verbatim by the daemon's HTTP routes.
    manager = LocalMemoryManager(postgres_db)
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
    assert result["success"] is True
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
