"""Apply and revert validated memory dream plans.

The GC sweep verdicts are ``keep | delete | refresh | review | promote``.
``review`` and ``delete`` soft-hide the row via ``mark_dreamed`` (recoverable,
snapshotted); ``refresh`` rewrites content in place; ``promote`` moves a
repo-scoped universal memory to global scope; ``keep`` only stamps the cooldown
cursor. Every candidate on a page is stamped ``last_dreamed_at`` — including
keeps and failed mutations — so the streaming sweep cursor always advances and
the row drops out of the next page.

Legacy ``merge``/``supersede`` handling is retained for old snapshots and
hand-built action lists; the validator no longer emits them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, cast

import psycopg

from gobby.memory.dream.models import DreamAction, DreamCandidate
from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.storage.memories import Memory

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
    when: str | None = None,
) -> dict[str, Any]:
    """Apply validated actions once, snapshotting before every mutation."""
    candidate_map = {candidate.id: candidate for candidate in candidates}
    summary = _empty_summary(dry_run)
    stamp = when or _now()
    snapshots_before = await asyncio.to_thread(store.count_snapshots, run_id)
    for action in actions:
        summary["actions"][action.action] = summary["actions"].get(action.action, 0) + 1
        if dry_run:
            summary["planned_actions"].append(_planned_action_preview(action, candidate_map))
            continue
        try:
            mutations = await _apply_action(
                memory_manager=memory_manager,
                store=store,
                run_id=run_id,
                action=action,
                candidate_map=candidate_map,
                stamp=stamp,
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
            # A failed mutation must not strand the candidate in the sweep window;
            # advance its cooldown cursor so it is not re-dreamed immediately.
            await _advance_cursor(
                memory_manager,
                store,
                run_id,
                candidate_map.get(action.memory_id or ""),
                stamp,
            )
        except Exception:
            logger.exception("Unexpected memory dream action failure")
            raise

    # Per-call delta, not the run-cumulative count: unit summaries are summed
    # by the sweep orchestrator, so a cumulative gauge here inflates totals.
    summary["snapshots"] = await asyncio.to_thread(store.count_snapshots, run_id) - snapshots_before
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
    """Conflict-aware restore for transactional stores; preserve protocol fakes."""
    if not hasattr(store.db, "transaction"):
        return await _revert_dream_run_legacy(
            store=store,
            run_id=run_id,
            memory_manager=memory_manager,
            reconcile_after_revert=reconcile_after_revert,
        )

    run = await asyncio.to_thread(store.get_run, run_id)
    if run is None:
        return {"success": False, "error": f"Dream run not found: {run_id}"}
    if run.get("status") == "reverted":
        return {"success": True, "run_id": run_id, "already_reverted": True}

    restored = 0
    deleted = 0
    conflicts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    secondary_failures: list[dict[str, str]] = []
    snapshots = await asyncio.to_thread(store.list_snapshots, run_id)
    for snapshot in snapshots:
        memory_id_value = snapshot.get("memory_id")
        if not memory_id_value:
            failures.append(
                {"snapshot_id": snapshot.get("id"), "error": "snapshot missing memory_id"}
            )
            continue
        memory_id = str(memory_id_value)
        before = snapshot.get("before_data")
        after = snapshot.get("after_data")
        try:
            if before is None and after is not None:
                if memory_manager is None:
                    raise ValueError("Dream-created memory revert requires a memory manager")
                removed = await memory_manager.delete_memory(memory_id)
                if removed:
                    deleted += 1
                continue

            outcome = await asyncio.to_thread(
                store.revert_snapshot,
                snapshot,
                on_committed=(
                    memory_manager.notify_memory_changed if memory_manager is not None else None
                ),
            )
            if outcome.status == "conflict":
                conflicts.append(
                    {
                        "snapshot_id": snapshot.get("id"),
                        "memory_id": memory_id,
                        "reason": "newer mutation owns action-restored columns",
                    }
                )
                continue
            if outcome.status == "missing" or outcome.row is None:
                continue

            restored += 1
            if memory_manager is not None:
                row = outcome.row
                try:
                    if snapshot.get("action") == "promote" and isinstance(after, dict):
                        secondary_failures.extend(
                            await memory_manager.sync_memory_scope_indices(
                                Memory.from_row(row),
                                previous_project_id=str(after["project_id"]),
                                previous_is_global=bool(after["is_global"]),
                                notify_changed=False,
                            )
                        )
                    else:
                        converged = await memory_manager.restore_memory_indices(
                            memory_id,
                            str(row["content"]),
                            str(row["project_id"]),
                            bool(row["is_global"]),
                            str(row["memory_type"]),
                            notify_changed=False,
                        )
                        if not converged:
                            secondary_failures.append(
                                {
                                    "memory_id": memory_id,
                                    "index": "secondary",
                                    "error": "not converged",
                                }
                            )
                except Exception as exc:
                    secondary_failures.append(
                        {"memory_id": memory_id, "index": "secondary", "error": str(exc)}
                    )
                    logger.warning("Memory dream revert reconciliation deferred: %s", exc)
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
            "conflicts": conflicts,
            "secondary_sync_failures": secondary_failures,
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
            **summary,
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
        "conflicts": conflicts,
    }
    if secondary_failures:
        result["secondary_sync_failures"] = secondary_failures
    if reconcile_after_revert and memory_manager is not None and (restored or deleted):
        await _reconcile(memory_manager, result)
    return result


async def _revert_dream_run_legacy(
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
    secondary_failures: list[dict[str, str]] = []
    failures: list[dict[str, Any]] = []
    snapshots = await asyncio.to_thread(store.list_snapshots, run_id)
    oldest_before_rows: dict[str, dict[str, Any]] = {}
    for snapshot in reversed(snapshots):
        before = snapshot.get("before_data")
        memory_id = snapshot.get("memory_id")
        if memory_id and isinstance(before, dict):
            oldest_before_rows.setdefault(str(memory_id), before)
    restored_ids: set[str] = set()
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
                if memory_manager is not None:
                    removed = await memory_manager.delete_memory(memory_id)
                    if not removed:
                        await asyncio.to_thread(store.delete_memory_row, memory_id)
                else:
                    await asyncio.to_thread(store.delete_memory_row, memory_id)
                deleted += 1
                continue
            if isinstance(before, dict):
                await asyncio.to_thread(store.restore_memory_row, before)
                restored_ids.add(memory_id)
                if memory_manager is not None:
                    if snapshot.get("action") == "promote":
                        secondary_failures.extend(
                            await memory_manager.sync_memory_scope_indices(Memory.from_row(before))
                        )
                    await memory_manager.restore_memory_indices(
                        memory_id,
                        str(before["content"]),
                        str(before["project_id"]),
                        bool(before["is_global"]),
                        str(before["memory_type"]),
                    )
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

    try:
        relationship_rows = [
            oldest_before_rows[memory_id]
            for memory_id in restored_ids
            if memory_id in oldest_before_rows
        ]
        if relationship_rows:
            await asyncio.to_thread(store.restore_crossrefs, relationship_rows)
    except Exception as exc:
        failures.append({"snapshot_id": None, "error": f"crossref restore failed: {exc}"})
        logger.warning("Memory dream crossref revert failed: %s", exc)

    completed_ts = _now()
    if failures:
        summary = {
            "restored": restored,
            "deleted_created_memories": deleted,
            "secondary_sync_failures": secondary_failures,
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
    if secondary_failures:
        result["secondary_sync_failures"] = secondary_failures
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
    stamp: str,
) -> int:
    if not hasattr(store.db, "transaction"):
        return await _apply_action_legacy(
            memory_manager=memory_manager,
            store=store,
            run_id=run_id,
            action=action,
            candidate_map=candidate_map,
            stamp=stamp,
        )
    if action.action == "refresh" and not action.content:
        await _advance_cursor(
            memory_manager,
            store,
            run_id,
            candidate_map.get(action.memory_id or ""),
            stamp,
        )
        return 0
    if action.action in {"keep", "review", "delete", "refresh", "promote"}:
        memory_id = _required_memory_id(action)
        candidate = candidate_map.get(memory_id)
        if candidate is None:
            logger.warning("Memory dream action skipped missing selected candidate: %s", memory_id)
            return 0
        return await _apply_fenced_action(
            memory_manager,
            store,
            run_id,
            candidate,
            action,
            stamp,
        )
    if action.action == "merge":
        return await _merge(memory_manager, store, run_id, action)
    if action.action == "supersede":
        _required_memory_id(action)
        return await _supersede(memory_manager, store, run_id, action, candidate_map)
    # Defensive fallback for any future action shape that reaches this dispatcher.
    await _advance_cursor(
        memory_manager,
        store,
        run_id,
        candidate_map.get(action.memory_id or ""),
        stamp,
    )
    return 0


async def _apply_action_legacy(
    *,
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    action: DreamAction,
    candidate_map: dict[str, DreamCandidate],
    stamp: str,
) -> int:
    """Retain deterministic behavior for non-transactional protocol test doubles."""
    if action.action == "keep":
        await _advance_cursor_legacy(memory_manager, action.memory_id, stamp)
        return 0
    if action.action in {"review", "delete"}:
        hide_action = cast(Literal["review", "delete"], action.action)
        return await _soft_hide_legacy(
            memory_manager,
            store,
            run_id,
            _required_memory_id(action),
            hide_action,
            stamp,
        )
    if action.action == "refresh" and action.content:
        return await _refresh_legacy(
            memory_manager,
            store,
            run_id,
            _required_memory_id(action),
            action,
            stamp,
        )
    if action.action == "promote":
        return await _promote_legacy(
            memory_manager,
            store,
            run_id,
            _required_memory_id(action),
            stamp,
        )
    if action.action == "merge":
        return await _merge(memory_manager, store, run_id, action)
    if action.action == "supersede":
        return await _supersede(memory_manager, store, run_id, action, candidate_map)
    await _advance_cursor_legacy(memory_manager, action.memory_id, stamp)
    return 0


async def _soft_hide_legacy(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    memory_id: str,
    action_name: Literal["review", "delete"],
    stamp: str,
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
    await asyncio.to_thread(
        memory_manager.mark_dreamed,
        memory_id,
        hidden_as=action_name,
        when=stamp,
    )
    after = await asyncio.to_thread(store.get_memory_row, memory_id)
    await asyncio.to_thread(store.complete_snapshot, snapshot_id, after_data=after)
    return 1


async def _promote_legacy(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    memory_id: str,
    stamp: str,
) -> int:
    before = await asyncio.to_thread(store.get_memory_row, memory_id)
    if before is None:
        return 0
    if bool(before["is_global"]):
        await _advance_cursor_legacy(memory_manager, memory_id, stamp)
        return 0
    snapshot_id = await asyncio.to_thread(
        store.insert_snapshot,
        run_id=run_id,
        memory_id=memory_id,
        action="promote",
        before_data=before,
    )
    try:
        await memory_manager.promote_memory(memory_id)
        await asyncio.to_thread(memory_manager.mark_dreamed, memory_id, when=stamp)
        after = await asyncio.to_thread(store.get_memory_row, memory_id)
        await asyncio.to_thread(store.complete_snapshot, snapshot_id, after_data=after)
        return 1
    except Exception:
        try:
            current = await asyncio.to_thread(store.get_memory_row, memory_id)
            if current is not None and bool(current["is_global"]) != bool(before["is_global"]):
                await _restore_promote_row(memory_manager, store, before)
        except Exception as rollback_exc:
            logger.warning(
                "Memory dream promote rollback restore failed: %s",
                rollback_exc,
                exc_info=True,
            )
        raise


async def _refresh_legacy(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    memory_id: str,
    action: DreamAction,
    stamp: str,
) -> int:
    before = await asyncio.to_thread(store.get_memory_row, memory_id)
    if before is None:
        return 0
    snapshot_id = await asyncio.to_thread(
        store.insert_snapshot,
        run_id=run_id,
        memory_id=memory_id,
        action="refresh",
        before_data=before,
    )
    await memory_manager.update_memory(
        memory_id=memory_id, content=action.content, tags=action.tags
    )
    await asyncio.to_thread(memory_manager.mark_dreamed, memory_id, when=stamp)
    after = await asyncio.to_thread(store.get_memory_row, memory_id)
    await asyncio.to_thread(store.complete_snapshot, snapshot_id, after_data=after)
    return 1


async def _advance_cursor_legacy(
    memory_manager: MemoryDreamManagerProtocol,
    memory_id: str | None,
    stamp: str,
) -> None:
    if not memory_id:
        return
    try:
        await asyncio.to_thread(memory_manager.mark_dreamed, memory_id, when=stamp)
    except _EXPECTED_ACTION_ERRORS as exc:
        logger.debug("Memory dream cursor advance skipped memory_id=%s: %s", memory_id, exc)


async def _apply_fenced_action(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    candidate: DreamCandidate,
    action: DreamAction,
    stamp: str,
) -> int:
    """Apply selected state atomically, then reconcile secondaries post-commit."""
    action_name = cast(
        Literal["keep", "review", "delete", "refresh", "promote"],
        action.action,
    )
    result = await asyncio.to_thread(
        store.apply_candidate_action,
        run_id=run_id,
        memory_id=candidate.id,
        action=action_name,
        selected_due_version=candidate.dream_due_version,
        selected_updated_at=candidate.updated_at,
        selected_project_id=candidate.project_id,
        selected_is_global=candidate.is_global,
        stamp=stamp,
        content=action.content,
        tags=action.tags,
        on_committed=memory_manager.notify_memory_changed,
    )
    if result is None:
        return 0
    if action_name == "refresh":
        await memory_manager.restore_memory_indices(
            candidate.id,
            str(result.after["content"]),
            str(result.after["project_id"]),
            bool(result.after["is_global"]),
            str(result.after["memory_type"]),
            notify_changed=False,
        )
    elif action_name == "promote":
        await memory_manager.sync_memory_scope_indices(
            Memory.from_row(result.after),
            previous_project_id=str(result.before["project_id"]),
            previous_is_global=bool(result.before["is_global"]),
            notify_changed=False,
        )
    return 0 if action_name == "keep" else 1


async def _advance_cursor(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    candidate: DreamCandidate | None,
    stamp: str,
) -> None:
    """Stamp ``last_dreamed_at`` for a kept candidate so the sweep cursor advances."""
    if candidate is None:
        return
    if not hasattr(store.db, "transaction"):
        await _advance_cursor_legacy(memory_manager, candidate.id, stamp)
        return
    try:
        await asyncio.to_thread(
            store.apply_candidate_action,
            run_id=run_id,
            memory_id=candidate.id,
            action="keep",
            selected_due_version=candidate.dream_due_version,
            selected_updated_at=candidate.updated_at,
            selected_project_id=candidate.project_id,
            selected_is_global=candidate.is_global,
            stamp=stamp,
            on_committed=memory_manager.notify_memory_changed,
        )
    except _EXPECTED_ACTION_ERRORS as exc:
        # Row vanished (e.g. concurrent delete); it drops out of the sweep naturally.
        logger.debug("Memory dream cursor advance skipped memory_id=%s: %s", candidate.id, exc)


async def _delete(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    memory_id: str,
    action_name: str,
) -> int:
    before = await asyncio.to_thread(store.get_memory_row, memory_id)
    if before is None:
        logger.warning(
            "Memory dream %s action skipped empty before snapshot for memory_id=%s",
            action_name,
            memory_id,
        )
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
        logger.warning(
            "Memory dream %s action produced empty after snapshot for memory_id=%s",
            action_name,
            memory_id,
        )
        return 0
    await asyncio.to_thread(store.complete_snapshot, snapshot_id, after_data=None)
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
            await asyncio.to_thread(store.restore_crossrefs, [before_keeper])
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
            keeper_before_transfer = await asyncio.to_thread(store.get_memory_row, keeper_id)
            await asyncio.to_thread(store.transfer_crossrefs, duplicate_id, keeper_id)
            deleted = await memory_manager.delete_memory(duplicate_id)
            if not deleted:
                relationship_rows = [before_duplicate]
                if keeper_before_transfer is not None:
                    relationship_rows.append(keeper_before_transfer)
                await asyncio.to_thread(store.restore_crossrefs, relationship_rows)
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
                is_global=candidate.is_global if candidate else False,
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


def _required_memory_id(action: DreamAction) -> str:
    memory_id = action.memory_id
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise ValueError(f"{action.action} action requires non-empty memory_id")
    return memory_id


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
    try:
        await asyncio.to_thread(store.restore_crossrefs, rows)
    except Exception as exc:
        logger.warning("Memory dream action rollback crossref restore failed: %s", exc)


async def _restore_promote_row(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    row: dict[str, Any],
) -> None:
    await asyncio.to_thread(store.restore_memory_row, row)
    memory = Memory.from_row(row)
    failures = await memory_manager.sync_memory_scope_indices(memory)
    if failures:
        logger.warning(
            "Memory dream promote rollback secondary sync failed for %s: %s",
            memory.id,
            failures,
        )


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
