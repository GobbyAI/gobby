"""Apply and revert validated memory dream plans.

The GC sweep verdicts are ``keep | delete | refresh | review | promote``.
``review`` and ``delete`` soft-hide the row via ``mark_dreamed`` (recoverable,
snapshotted); ``refresh`` rewrites content in place; ``promote`` moves a
repo-scoped universal memory to global scope; ``keep`` only stamps the cooldown
cursor. Every candidate on a page is stamped ``last_dreamed_at`` — including
keeps and failed mutations — so the streaming sweep cursor always advances and
the row drops out of the next page.

"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import psycopg

from gobby.memory.dream.models import DreamAction, DreamCandidate
from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.storage.memories import Memory
from gobby.storage.memories_crud import DuplicateMemoryContentError

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
            if isinstance(exc, DuplicateMemoryContentError):
                # Benign self-healing collision: the refreshed content already
                # exists live in the same scope, so skipping the rewrite loses
                # nothing — the cooldown advance below is the whole remedy.
                logger.info("Memory dream action skipped duplicate content: %s", exc)
            else:
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
    snapshots_after = await asyncio.to_thread(store.count_snapshots, run_id)
    summary["snapshots"] = snapshots_after - snapshots_before
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
    """Conflict-aware restore for transactional stores."""
    run = await asyncio.to_thread(store.get_run, run_id)
    if run is None:
        return {"success": False, "error": f"Dream run not found: {run_id}"}
    if run.get("status") == "revert_forfeited":
        return {
            "success": False,
            "run_id": run_id,
            "status": "revert_forfeited",
            "error": "Dream run cannot be reverted because its snapshots were forfeited",
        }
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


async def _apply_action(
    *,
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    action: DreamAction,
    candidate_map: dict[str, DreamCandidate],
    stamp: str,
) -> int:
    if action.action == "refresh" and not action.content:
        await _advance_cursor(
            memory_manager,
            store,
            run_id,
            candidate_map.get(action.memory_id or ""),
            stamp,
        )
        return 0
    if action.action == "keep" and not (action.memory_id or "").strip():
        # Audit-only keep marker from plan validation (planner referenced an
        # unknown or missing candidate id) — nothing to stamp, not a failure.
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
    # Defensive fallback for any future action shape that reaches this dispatcher.
    await _advance_cursor(
        memory_manager,
        store,
        run_id,
        candidate_map.get(action.memory_id or ""),
        stamp,
    )
    return 0


async def _apply_fenced_action(
    memory_manager: MemoryDreamManagerProtocol,
    store: MemoryDreamStore,
    run_id: str,
    candidate: DreamCandidate,
    action: DreamAction,
    stamp: str,
) -> int:
    """Apply selected state atomically, then reconcile secondaries post-commit."""
    action_name = action.action
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


async def _reconcile(memory_manager: MemoryDreamManagerProtocol, summary: dict[str, Any]) -> None:
    try:
        summary["reconcile"] = await memory_manager.reconcile_stores(dry_run=False)
    except Exception as exc:  # Reconciliation must preserve visibility of applied snapshots.
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


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
