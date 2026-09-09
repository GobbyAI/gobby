"""PostgreSQL contracts for Dream's authoritative decision evidence."""

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from gobby.memory.dream.apply import apply_dream_plan
from gobby.memory.dream.candidates import memory_to_candidate
from gobby.memory.dream.decisions import DreamDecisionStore
from gobby.memory.dream.models import DreamAction, DreamCandidate
from gobby.memory.dream.plan import validate_dream_plan
from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.integration


def _selected(db: HubDatabase, *, global_scope: bool = False) -> DreamCandidate:
    memory = LocalMemoryManager(db).create_memory(
        content="Use portable paths", project_id=PERSONAL_PROJECT_ID, is_global=global_scope
    )
    db.execute("UPDATE memories SET vector_needs_reindex = FALSE WHERE id = %s", (memory.id,))
    return memory_to_candidate(memory, datetime.now(UTC))


def _manager() -> Mock:
    manager = Mock(spec=MemoryDreamManagerProtocol)
    manager.sync_memory_scope_indices = AsyncMock()
    manager.restore_memory_indices = AsyncMock()
    return manager


async def _apply(
    db: HubDatabase, candidate: DreamCandidate, action: DreamAction, manager: Mock
) -> tuple[str, dict[str, object]]:
    store = MemoryDreamStore(db)
    run_id = store.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})
    summary = await apply_dream_plan(
        memory_manager=cast(MemoryDreamManagerProtocol, manager),
        store=store,
        run_id=run_id,
        actions=[action],
        candidates=[candidate],
        dry_run=False,
        reconcile_after_apply=False,
    )
    return run_id, summary


@pytest.mark.parametrize("global_scope", [True, False])
async def test_promotion_evidence_and_effective_changes(
    temp_db: HubDatabase, global_scope: bool
) -> None:
    candidate = _selected(temp_db, global_scope=global_scope)
    proposal = {
        "action": "promote",
        "memory_id": candidate.id,
        "reason": "portable",
        "confidence": 1,
    }
    actions = validate_dream_plan(
        {"actions": [proposal]},
        [candidate],
        min_action_confidence=0.5,
        min_delete_confidence=0.9,
        min_rescope_confidence=0.9,
    )
    manager = _manager()
    run_id, summary = await _apply(temp_db, candidate, actions[0], manager)
    decision = DreamDecisionStore(temp_db).page(run_id)["decisions"][0]
    assert decision["proposals"][0]["proposal"] == proposal
    assert decision["candidate"]["is_global"] is global_scope
    assert decision["effective_action"]["action"] == ("keep" if global_scope else "promote")
    assert decision["status"] == ("noop" if global_scope else "applied")
    assert decision["snapshot_id"] == MemoryDreamStore(temp_db).list_snapshots(run_id)[0]["id"]
    assert summary["mutations"] == int(not global_scope)
    assert summary["noops"] == int(global_scope)
    assert summary["proposed_actions"] == {"promote": 1}
    row = temp_db.fetchone(
        "SELECT updated_at, vector_needs_reindex FROM memories WHERE id = %s", (candidate.id,)
    )
    assert row is not None and row["updated_at"] == candidate.updated_at
    assert row["vector_needs_reindex"] is not global_scope
    assert manager.sync_memory_scope_indices.await_count == int(not global_scope)
    assert manager.notify_memory_changed.call_count == int(not global_scope)


async def test_projection_failure_retains_applied_outcome(temp_db: HubDatabase) -> None:
    candidate = _selected(temp_db)
    manager = _manager()
    manager.sync_memory_scope_indices.side_effect = OSError("index unavailable")
    run_id, summary = await _apply(
        temp_db, candidate, DreamAction(action="promote", memory_id=candidate.id), manager
    )
    decision = DreamDecisionStore(temp_db).page(run_id)["decisions"][0]
    assert summary["mutations"] == 1
    assert summary["errors"] == 1
    assert decision["status"] == "applied"
    assert decision["outcome"] == {"mutations": 1, "error": "index unavailable"}
    assert len(MemoryDreamStore(temp_db).list_snapshots(run_id)) == 1


async def test_conflict_records_skipped_without_overwriting_memory(temp_db: HubDatabase) -> None:
    candidate = _selected(temp_db)
    temp_db.execute(
        "UPDATE memories SET dream_due_version = dream_due_version + 1 WHERE id = %s",
        (candidate.id,),
    )
    run_id, summary = await _apply(
        temp_db, candidate, DreamAction(action="delete", memory_id=candidate.id), _manager()
    )
    decision = DreamDecisionStore(temp_db).page(run_id)["decisions"][0]
    assert summary["mutations"] == 0 and summary["skipped"] == 1
    assert decision["status"] == "skipped"
    assert "changed" in decision["outcome"]["reason"]
    assert decision["snapshot_id"] is None
    assert LocalMemoryManager(temp_db).get_memory(candidate.id).deleted_at is None


def test_pagination_and_restart_preserve_all_proposals(temp_db: HubDatabase) -> None:
    candidate = _selected(temp_db)
    raw = {
        "actions": [
            None,
            {
                "action": "delete",
                "memory_id": "unknown",
                "reason": "bad reference",
                "confidence": 1,
            },
        ]
    }
    actions = validate_dream_plan(
        raw,
        [candidate],
        min_action_confidence=0.5,
        min_delete_confidence=0.9,
        min_rescope_confidence=0.9,
    )
    store = MemoryDreamStore(temp_db)
    run_id = store.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})
    ledger = DreamDecisionStore(temp_db)
    ledger.stage(run_id, actions, [candidate])
    assert run_id in store.mark_interrupted_runs()
    page = ledger.page(run_id, limit=1)
    items = page["decisions"]
    while page["next_offset"] is not None:
        page = ledger.page(run_id, offset=page["next_offset"], limit=1)
        items.extend(page["decisions"])
    proposals = {
        p["ordinal"]: p["proposal"] for d in items for p in d["proposals"] if "proposal" in p
    }
    assert list(proposals.values()) == raw["actions"]
    assert all(d["status"] == "interrupted" for d in items)
    assert len({d["id"] for d in items}) == len(actions)


def test_deleted_memory_not_reindexed_until_restored(temp_db: HubDatabase) -> None:
    candidate = _selected(temp_db)
    manager = LocalMemoryManager(temp_db)
    manager.mark_dreamed(candidate.id, hidden_as="delete")
    temp_db.execute(
        "UPDATE memories SET vector_needs_reindex = TRUE WHERE id = %s", (candidate.id,)
    )
    assert candidate.id not in manager.list_vector_reindex_ids()
    manager.restore_memory(candidate.id)
    assert candidate.id in manager.list_vector_reindex_ids()
