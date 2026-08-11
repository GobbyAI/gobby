"""Fenced memory dream apply and revert transactions."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from gobby.memory.dream.storage_journal import (
    capture_crossrefs,
    complete_snapshot,
    insert_snapshot,
    restore_crossrefs,
)
from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories_crud import DuplicateMemoryContentError
from gobby.utils.datetime import utc_now
from gobby.utils.json_helpers import json_dumps


@dataclass(frozen=True, slots=True)
class DreamApplyResult:
    """Rows captured by one committed fenced dream action."""

    snapshot_id: int
    before: dict[str, Any]
    after: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DreamRevertResult:
    """Outcome of one snapshot's conflict-aware primary restore."""

    status: Literal["restored", "conflict", "missing"]
    memory_id: str
    row: dict[str, Any] | None = None


class _DreamActionHost(Protocol):
    db: HubDatabase


class _DreamActionMixin:
    def apply_candidate_action(
        self: _DreamActionHost,
        *,
        run_id: str,
        memory_id: str,
        action: Literal["keep", "review", "delete", "refresh", "promote"],
        selected_due_version: int,
        selected_updated_at: datetime,
        selected_project_id: str,
        selected_is_global: bool,
        stamp: str,
        content: str | None = None,
        tags: list[str] | None = None,
        on_committed: Callable[[], None] | None = None,
    ) -> DreamApplyResult | None:
        """Apply one dream action behind the complete selected-row fence."""
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM memories
                 WHERE id = %s
                   AND dream_due_version = %s
                   AND updated_at = %s
                   AND project_id = %s
                   AND is_global = %s
                   AND deleted_at IS NULL
                 FOR UPDATE
                """,
                (
                    memory_id,
                    selected_due_version,
                    selected_updated_at,
                    selected_project_id,
                    selected_is_global,
                ),
            ).fetchone()
            if row is None:
                return None

            before = dict(row)
            before["tags"] = _decode(before.get("tags")) or []
            before["_crossrefs"] = capture_crossrefs(conn, memory_id)
            snapshot_id = insert_snapshot(
                conn,
                run_id=run_id,
                memory_id=memory_id,
                action=action,
                before_data=before,
            )

            if action == "keep":
                conn.execute(
                    "UPDATE memories SET last_dreamed_at = %s WHERE id = %s",
                    (stamp, memory_id),
                )
            elif action in {"review", "delete"}:
                conn.execute(
                    """
                    UPDATE memories
                       SET last_dreamed_at = %s, deleted_at = %s, dream_action = %s
                     WHERE id = %s
                    """,
                    (stamp, stamp, action, memory_id),
                )
            elif action == "refresh":
                normalized_content = (content or "").strip()
                if not normalized_content:
                    raise ValueError("Memory content cannot be empty")
                duplicate = conn.execute(
                    """
                    SELECT id FROM memories
                     WHERE content = %s
                       AND project_id = %s
                       AND is_global = %s
                       AND id != %s
                       AND deleted_at IS NULL
                     LIMIT 1
                    """,
                    (normalized_content, selected_project_id, selected_is_global, memory_id),
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateMemoryContentError(
                        "Memory content already exists in this project/global scope"
                    )
                if tags is None:
                    conn.execute(
                        """
                        UPDATE memories
                           SET content = %s, updated_at = %s, last_dreamed_at = %s,
                               vector_needs_reindex = TRUE
                         WHERE id = %s
                        """,
                        (normalized_content, utc_now(), stamp, memory_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE memories
                           SET content = %s, tags = %s, updated_at = %s,
                               last_dreamed_at = %s, vector_needs_reindex = TRUE
                         WHERE id = %s
                        """,
                        (normalized_content, json_dumps(tags), utc_now(), stamp, memory_id),
                    )
            elif action == "promote":
                conn.execute(
                    """
                    UPDATE memories
                       SET is_global = TRUE, vector_needs_reindex = TRUE,
                           last_dreamed_at = %s
                     WHERE id = %s
                    """,
                    (stamp, memory_id),
                )
            else:
                raise ValueError(f"Unsupported dream action: {action}")

            if action in {"review", "delete"}:
                EmbeddingGenerationState(self.db).append_change(
                    "memory", memory_id, is_tombstone=True, transaction=conn
                )
            elif action in {"refresh", "promote"}:
                EmbeddingGenerationState(self.db).append_change(
                    "memory", memory_id, transaction=conn
                )

            after_row = conn.execute(
                "SELECT * FROM memories WHERE id = %s", (memory_id,)
            ).fetchone()
            if after_row is None:
                raise RuntimeError(f"Memory {memory_id} vanished during dream apply")
            after = dict(after_row)
            after["tags"] = _decode(after.get("tags")) or []
            after["_crossrefs"] = capture_crossrefs(conn, memory_id)
            complete_snapshot(
                conn,
                snapshot_id,
                after_data=after,
            )

        if on_committed is not None:
            on_committed()
        return DreamApplyResult(snapshot_id=snapshot_id, before=before, after=after)

    def revert_snapshot(
        self: _DreamActionHost,
        snapshot: dict[str, Any],
        *,
        on_committed: Callable[[], None] | None = None,
    ) -> DreamRevertResult:
        """Restore one snapshot only if its action-owned after-state still owns the row."""
        memory_id = str(snapshot["memory_id"])
        action = str(snapshot["action"])
        before = snapshot.get("before_data")
        after = snapshot.get("after_data")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError(f"Snapshot {snapshot.get('id')} lacks restorable row data")

        owned_columns: tuple[str, ...]
        if action == "refresh":
            owned_columns = ("content", "tags")
        elif action == "promote":
            owned_columns = ("is_global",)
        elif action in {"review", "delete"}:
            owned_columns = ("deleted_at", "dream_action")
        else:
            owned_columns = ()

        with self.db.transaction() as conn:
            current_row = conn.execute(
                "SELECT * FROM memories WHERE id = %s FOR UPDATE",
                (memory_id,),
            ).fetchone()
            if current_row is None:
                return DreamRevertResult(status="missing", memory_id=memory_id)
            current = dict(current_row)
            current["tags"] = _decode(current.get("tags")) or []
            fence_columns = tuple(
                dict.fromkeys(("deleted_at", "project_id", "is_global", *owned_columns))
            )
            if any(
                not _same_snapshot_value(current.get(column), after.get(column))
                for column in fence_columns
            ):
                return DreamRevertResult(status="conflict", memory_id=memory_id)

            assignments: list[str] = []
            params: list[Any] = []
            for column in owned_columns:
                assignments.append(f"{column} = %s")
                value = before.get(column)
                params.append(json_dumps(value) if column == "tags" else value)
            assignments.extend(
                [
                    "updated_at = %s",
                    "last_dreamed_at = NULL",
                    "dream_due_version = dream_due_version + 1",
                    "vector_needs_reindex = TRUE",
                ]
            )
            params.extend((utc_now(), memory_id))
            conn.execute(
                f"UPDATE memories SET {', '.join(assignments)} WHERE id = %s",  # nosec B608
                tuple(params),
            )
            restore_crossrefs(
                conn,
                memory_id,
                list(before.get("_crossrefs") or []),
            )
            restored_row = conn.execute(
                "SELECT * FROM memories WHERE id = %s",
                (memory_id,),
            ).fetchone()
            if restored_row is None:
                raise RuntimeError(f"Memory {memory_id} vanished during dream revert")
            restored = dict(restored_row)
            restored["tags"] = _decode(restored.get("tags")) or []
            EmbeddingGenerationState(self.db).append_change(
                "memory",
                memory_id,
                is_tombstone=restored.get("deleted_at") is not None,
                transaction=conn,
            )

        if on_committed is not None:
            on_committed()
        return DreamRevertResult(status="restored", memory_id=memory_id, row=restored)


def _same_snapshot_value(current: Any, captured: Any) -> bool:
    return json_dumps(current, default=str, sort_keys=True) == json_dumps(
        captured,
        default=str,
        sort_keys=True,
    )


def _decode(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value
