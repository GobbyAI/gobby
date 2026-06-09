"""Apply and revert validated memory dream plans."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import psycopg

from gobby.memory.dream.models import DreamAction, DreamCandidate
from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.storage import MemoryDreamStore

logger = logging.getLogger(__name__)
_EXPECTED_ACTION_ERRORS = (ValueError, OSError, psycopg.Error)


async def apply_dream_plan(
    *,
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    actions: list[DreamAction],
    candidates: list[DreamCandidate],
    dry_run: bool,
    reconcile_after_apply: bool,
) -> dict[str, Any]:
    """Apply validated actions once, with snapshots before every mutation."""
    candidate_map = {candidate.id: candidate for candidate in candidates}
    summary = _empty_summary(dry_run)
    for action in actions:
        summary["actions"][action.action] = summary["actions"].get(action.action, 0) + 1
        if dry_run:
            summary["planned_actions"].append(_planned_action_preview(action, candidate_map))
            continue
        if action.action in {"keep", "review"}:
            continue
        try:
            mutations = await _apply_action(
                memory_manager=memory_manager,
                store=store,
                run_id=run_id,
                action=action,
                candidate_map=candidate_map,
            )
            summary["mutations"] += mutations
        except _EXPECTED_ACTION_ERRORS as exc:
            summary["errors"] += 1
            summary["error_details"].append(
                {
                    "action": action.to_dict(),
                    "error": str(exc),
                }
            )
            logger.warning("Memory dream action failed: %s", exc)
        except Exception:
            logger.exception("Unexpected memory dream action failure")
            raise

    summary["snapshots"] = len(await asyncio.to_thread(store.list_snapshots, run_id))
    if summary["mutations"] and reconcile_after_apply:
        await _reconcile(memory_manager, summary)
    return summary


async def revert_dream_run(
    *,
    store: MemoryDreamStore,
    run_id: str,
    memory_manager: MemoryDreamManagerProtocol | None = None,
    reconcile_after_revert: bool = True,
) -> dict[str, Any]:
    """Restore memory rows from a dream run's snapshots in reverse order."""
    run = await asyncio.to_thread(store.get_run, run_id)
    if run is None:
        return {"success": False, "error": f"Dream run not found: {run_id}"}
    if run.get("status") == "reverted":
        return {"success": True, "run_id": run_id, "already_reverted": True}

    restored = 0
    deleted = 0
    failures: list[dict[str, Any]] = []
    snapshots = await asyncio.to_thread(store.list_snapshots, run_id)
    for snapshot in snapshots:
        memory_id_value = snapshot.get("memory_id")
        if not memory_id_value:
            failures.append(
                {
                    "snapshot_id": snapshot.get("id"),
                    "error": "snapshot missing memory_id",
                }
            )
            continue
        memory_id = str(memory_id_value)
        try:
            before = snapshot.get("before_data")
            after = snapshot.get("after_data")
            if before is None and after is not None:
                await asyncio.to_thread(store.delete_memory_row, memory_id)
                deleted += 1
                continue
            if isinstance(before, dict):
                await asyncio.to_thread(store.restore_memory_row, before)
                restored += 1
        except Exception as exc:
            failures.append(
                {
                    "snapshot_id": snapshot.get("id"),
                    "memory_id": memory_id,
                    "error": str(exc),
                }
            )
            logger.warning("Memory dream snapshot revert failed: %s", exc)

    completed_ts = _now()
    if failures:
        summary = {
            "restored": restored,
            "deleted_created_memories": deleted,
            "errors": len(failures),
            "error_details": failures,
        }
        error = f"Failed to revert {len(failures)} memory dream snapshot(s)"
        await asyncio.to_thread(
            store.update_run,
            run_id,
            status="revert_failed",
            completed_at=completed_ts,
            summary=summary,
            error=error,
        )
        return {
            "success": False,
            "run_id": run_id,
            "status": "revert_failed",
            "restored": restored,
            "deleted_created_memories": deleted,
            "errors": len(failures),
            "error_details": failures,
            "error": error,
        }

    await asyncio.to_thread(
        store.update_run,
        run_id,
        status="reverted",
        reverted_at=completed_ts,
        completed_at=completed_ts,
    )
    result: dict[str, Any] = {
        "success": True,
        "run_id": run_id,
        "restored": restored,
        "deleted_created_memories": deleted,
    }
    if reconcile_after_revert and memory_manager is not None and (restored or deleted):
        await _reconcile(memory_manager, result)
    return result


async def _apply_action(
    *,
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    action: DreamAction,
    candidate_map: dict[str, DreamCandidate],
) -> int:
    if action.action == "delete" and action.memory_id:
        return await _delete(memory_manager, store, run_id, action.memory_id, "delete")
    if action.action == "refresh" and action.memory_id and action.content:
        return await _refresh(memory_manager, store, run_id, action)
    if action.action == "merge":
        return await _merge(memory_manager, store, run_id, action)
    if action.action == "supersede" and action.memory_id:
        return await _supersede(memory_manager, store, run_id, action, candidate_map)
    return 0


async def _delete(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    memory_id: str,
    action_name: str,
) -> int:
    before = await asyncio.to_thread(store.get_memory_row, memory_id)
    if before is None:
        return 0
    snapshot_id = await asyncio.to_thread(
        store.insert_snapshot,
        run_id=run_id,
        memory_id=memory_id,
        action=action_name,
        before_data=before,
    )
    deleted = await memory_manager.delete_memory(memory_id)
    if not deleted:
        return 0
    await asyncio.to_thread(store.complete_snapshot, snapshot_id, after_data=None)
    return 1


async def _refresh(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    action: DreamAction,
) -> int:
    if action.memory_id is None:
        raise ValueError("refresh action requires memory_id")
    before = await asyncio.to_thread(store.get_memory_row, action.memory_id)
    if before is None:
        return 0
    snapshot_id = await asyncio.to_thread(
        store.insert_snapshot,
        run_id=run_id,
        memory_id=action.memory_id,
        action="refresh",
        before_data=before,
    )
    await memory_manager.update_memory(
        memory_id=action.memory_id,
        content=action.content,
        tags=action.tags,
    )
    after = await asyncio.to_thread(store.get_memory_row, action.memory_id)
    await asyncio.to_thread(store.complete_snapshot, snapshot_id, after_data=after)
    return 1


async def _merge(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    action: DreamAction,
) -> int:
    if len(action.memory_ids) < 2 or not action.content:
        return 0
    keeper_id = action.memory_ids[0]
    mutations = 0
    rollback_rows: list[dict[str, Any]] = []
    pending_snapshots: list[tuple[int, dict[str, Any] | None]] = []
    before_keeper = await asyncio.to_thread(store.get_memory_row, keeper_id)
    try:
        if before_keeper is not None and before_keeper.get("content") != action.content:
            snapshot_id = await asyncio.to_thread(
                store.insert_snapshot,
                run_id=run_id,
                memory_id=keeper_id,
                action="merge",
                before_data=before_keeper,
            )
            await memory_manager.update_memory(
                memory_id=keeper_id,
                content=action.content,
                tags=action.tags,
            )
            after_keeper = await asyncio.to_thread(store.get_memory_row, keeper_id)
            pending_snapshots.append((snapshot_id, after_keeper))
            rollback_rows.append(before_keeper)
            mutations += 1

        for duplicate_id in action.memory_ids[1:]:
            before_duplicate = await asyncio.to_thread(store.get_memory_row, duplicate_id)
            if before_duplicate is None:
                continue
            snapshot_id = await asyncio.to_thread(
                store.insert_snapshot,
                run_id=run_id,
                memory_id=duplicate_id,
                action="merge",
                before_data=before_duplicate,
            )
            rollback_rows.append(before_duplicate)
            deleted = await memory_manager.delete_memory(duplicate_id)
            if not deleted:
                continue
            pending_snapshots.append((snapshot_id, None))
            mutations += 1

        for snapshot_id, after_data in pending_snapshots:
            await asyncio.to_thread(store.complete_snapshot, snapshot_id, after_data=after_data)
        return mutations
    except Exception:
        await _restore_rows_for_failed_action(store, rollback_rows)
        raise


async def _supersede(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    action: DreamAction,
    candidate_map: dict[str, DreamCandidate],
) -> int:
    if action.memory_id is None:
        raise ValueError("supersede action requires memory_id")
    mutations = 0
    target_exists = False
    created_id: str | None = None
    deleted_rows: list[dict[str, Any]] = []
    if action.target_id:
        target_exists = await asyncio.to_thread(store.get_memory_row, action.target_id) is not None
    try:
        if action.content and not target_exists:
            candidate = candidate_map.get(action.memory_id)
            created = await memory_manager.create_memory(
                content=action.content,
                memory_type=action.memory_type or (candidate.memory_type if candidate else "fact"),
                project_id=candidate.project_id if candidate else None,
                source_type="agent",
                tags=action.tags or (candidate.tags if candidate else None),
            )
            created_id = str(created.id)
            after = await asyncio.to_thread(store.get_memory_row, created_id)
            await asyncio.to_thread(
                store.record_applied_snapshot,
                run_id=run_id,
                memory_id=created_id,
                action="supersede",
                before_data=None,
                after_data=after,
            )
            mutations += 1
        before_deleted = await asyncio.to_thread(store.get_memory_row, action.memory_id)
        if before_deleted is not None:
            deleted_rows.append(before_deleted)
        deleted = await _delete(memory_manager, store, run_id, action.memory_id, "supersede")
        mutations += deleted
        return mutations
    except Exception:
        if created_id is not None:
            try:
                await memory_manager.delete_memory(created_id)
            except Exception as exc:
                logger.warning("Memory dream supersede rollback delete failed: %s", exc)
        await _restore_rows_for_failed_action(store, deleted_rows)
        raise


async def _reconcile(memory_manager: MemoryDreamManagerProtocol, summary: dict[str, Any]) -> None:
    try:
        summary["reconcile"] = await memory_manager.reconcile_stores(dry_run=False)
    except Exception as exc:  # noqa: BLE001 - reconciliation must not hide applied snapshots
        summary["reconcile_error"] = str(exc)
        logger.warning("Memory dream reconcile failed: %s", exc)


def _empty_summary(dry_run: bool) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "actions": {},
        "mutations": 0,
        "snapshots": 0,
        "errors": 0,
        "error_details": [],
    }
    if dry_run:
        summary["planned_actions"] = []
    return summary


def _planned_action_preview(
    action: DreamAction,
    candidate_map: dict[str, DreamCandidate],
) -> dict[str, Any]:
    affected_ids = sorted(action.affected_ids())
    candidates = [
        candidate_map[memory_id].to_prompt_dict()
        for memory_id in affected_ids
        if memory_id in candidate_map
    ]
    return {
        "action": action.to_dict(),
        "affected_ids": affected_ids,
        "candidates": candidates,
    }


async def _restore_rows_for_failed_action(
    store: MemoryDreamStore,
    rows: list[dict[str, Any]],
) -> None:
    for row in reversed(rows):
        try:
            await asyncio.to_thread(store.restore_memory_row, row)
        except Exception as exc:
            logger.warning("Memory dream action rollback restore failed: %s", exc)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
