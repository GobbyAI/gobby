"""Deterministic JSONL backup and explicit restore for memories."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from gobby.config.persistence import MemoryBackupConfig
from gobby.memory.manager import MemoryManager
from gobby.memory.write_result import MemoryWriteResult
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import ALL_MEMORIES, Memory, MemoryScope, validate_memory_type
from gobby.sync.jsonl_io import atomic_write_text, export_file_lock, project_backup_path
from gobby.utils.datetime import datetime_to_iso, parse_stored_datetime
from gobby.utils.json_helpers import json_dumps

__all__ = [
    "MemoryBackupError",
    "MemoryBackupManager",
    "MemoryRestoreError",
]

logger = logging.getLogger(__name__)


class MemoryRestoreError(RuntimeError):
    """Raised when a memory backup cannot be restored safely."""


class MemoryBackupError(RuntimeError):
    """Raised when a memory backup cannot be written."""


def _parse_memory_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, datetime | str) or value == "":
        return None
    try:
        return parse_stored_datetime(value)
    except ValueError:
        return None


def _jsonl_memory_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    for field in ("created_at", "updated_at"):
        parsed = _parse_memory_timestamp(normalized.get(field))
        if parsed is not None:
            normalized[field] = datetime_to_iso(parsed)
    return normalized


_EPHEMERAL_IMPLEMENTATION_TAGS = {"build-e2e"}
_EPHEMERAL_CONTENT_MARKERS = ("build #epic", "docs test #", "merged into local")


def is_ephemeral_implementation_note(record: Mapping[str, Any]) -> bool:
    """Return True for run-specific implementation notes rejected at creation time."""
    memory_type = str(record.get("type") or record.get("memory_type") or "").lower()
    if memory_type != "implementation_note":
        return False

    raw_tags = record.get("tags")
    tags = (
        {str(tag).strip().lower() for tag in raw_tags if str(tag).strip()}
        if isinstance(raw_tags, list)
        else set()
    )
    if tags & _EPHEMERAL_IMPLEMENTATION_TAGS:
        return True
    if any(tag.startswith("#") and tag[1:].isdigit() for tag in tags):
        return True

    content = str(record.get("content") or "").lower()
    return any(marker in content for marker in _EPHEMERAL_CONTENT_MARKERS)


def _validate_uuid(value: Any, *, field: str, line_num: int, required: bool = False) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str):
        raise MemoryRestoreError(f"Memory backup line {line_num}: {field} must be a UUID string")
    try:
        UUID(value)
    except ValueError as exc:
        raise MemoryRestoreError(
            f"Memory backup line {line_num}: {field} is not a valid UUID: {value}"
        ) from exc


def _validate_memory_record(data: dict[str, Any], line_num: int) -> dict[str, Any]:
    if "_deleted" in data:
        raise MemoryRestoreError(
            f"Memory backup line {line_num}: tombstone records are not supported"
        )

    memory_id = data.get("id")
    _validate_uuid(memory_id, field="id", line_num=line_num, required=True)

    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise MemoryRestoreError(f"Memory backup line {line_num}: content must be non-empty")

    memory_type = data.get("type")
    if not isinstance(memory_type, str) or not memory_type:
        raise MemoryRestoreError(f"Memory backup line {line_num}: type must be non-empty")
    try:
        data["type"] = validate_memory_type(memory_type).value
    except ValueError as exc:
        raise MemoryRestoreError(f"Memory backup line {line_num}: {exc}") from exc

    tags = data.get("tags")
    if tags is not None and (
        not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags)
    ):
        raise MemoryRestoreError(f"Memory backup line {line_num}: tags must be a list of strings")

    for field in ("created_at", "updated_at"):
        parsed = _parse_memory_timestamp(data.get(field))
        if parsed is None:
            raise MemoryRestoreError(f"Memory backup line {line_num}: invalid {field} timestamp")
        data[field] = parsed

    source = data.get("source", "agent")
    if source not in ("user", "agent"):
        raise MemoryRestoreError(f"Memory backup line {line_num}: source must be 'user' or 'agent'")

    _validate_uuid(data.get("project_id"), field="project_id", line_num=line_num, required=True)
    if not isinstance(data.get("is_global"), bool):
        raise MemoryRestoreError(f"Memory backup line {line_num}: is_global must be a boolean")
    _validate_uuid(data.get("source_id"), field="source_id", line_num=line_num)
    _validate_uuid(data.get("source_task_id"), field="source_task_id", line_num=line_num)
    return data


def _load_memory_backup(path: Path) -> list[dict[str, Any]]:
    """Parse and validate an entire memory backup before database writes begin."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MemoryRestoreError(f"Failed to read memory backup {path}: {exc}") from exc

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_num, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MemoryRestoreError(f"Invalid JSON in memory backup on line {line_num}") from exc
        if not isinstance(parsed, dict):
            raise MemoryRestoreError(f"Memory backup line {line_num}: record must be an object")

        record = _validate_memory_record(parsed, line_num)
        memory_id = record["id"]
        if memory_id in seen_ids:
            raise MemoryRestoreError(f"Memory backup line {line_num}: duplicate id {memory_id}")
        seen_ids.add(memory_id)
        records.append(record)

    return records


class MemoryBackupManager:
    """Create live-row snapshots and restore them with last-write-wins semantics."""

    def __init__(
        self,
        db: HubDatabase,
        memory_manager: MemoryManager | None,
        config: MemoryBackupConfig,
    ) -> None:
        self.db = db
        self.memory_manager = memory_manager
        self.config = config
        self.backup_path = config.backup_path
        self._custom_backup_path = config.backup_path != Path(".gobby/memories.jsonl")

    def _get_backup_path(self, project_id: str | None = None) -> Path:
        if project_id and not self._custom_backup_path:
            return project_backup_path(project_id, "memories.jsonl")

        if self.backup_path.is_absolute():
            return self.backup_path

        try:
            from gobby.utils.project_context import get_project_context

            project_ctx = get_project_context()
            if project_ctx and project_ctx.get("project_path"):
                project_path = Path(project_ctx["project_path"]).expanduser().resolve()
                return project_path / self.backup_path
        except Exception as exc:
            logger.debug("Fallback to cwd since project context unavailable: %s", exc)

        return Path.cwd() / self.backup_path

    async def backup(self, project_id: str | None = None) -> int:
        """Write a backup without blocking the caller's event loop."""
        if not self.config.enabled or self.memory_manager is None:
            return 0
        return await asyncio.to_thread(self.backup_sync, project_id)

    def backup_sync(self, project_id: str | None = None) -> int:
        """Write current live memory rows as a deterministic atomic JSONL snapshot."""
        if not self.config.enabled or self.memory_manager is None:
            return 0
        try:
            return self._backup_memories_sync(
                self._get_backup_path(project_id), project_id=project_id
            )
        except MemoryBackupError:
            raise
        except Exception as exc:
            logger.exception("Failed to back up memories: %s", exc)
            raise MemoryBackupError(f"Failed to back up memories: {exc}") from exc

    async def restore(self) -> int:
        """Restore a backup without blocking the caller's event loop."""
        memory_manager = self.memory_manager
        if not self.config.enabled or memory_manager is None:
            return 0
        path = self._get_backup_path()
        if not path.is_file():
            return 0

        async def restore_owned() -> int:
            restored_count, outcomes = await asyncio.to_thread(
                self._restore_memories_with_outcomes_sync,
                path,
            )
            semaphore = asyncio.Semaphore(8)

            async def reconcile(
                result: MemoryWriteResult[Memory],
            ) -> asyncio.Task[None] | None:
                async with semaphore:
                    await memory_manager.reconcile_memory_indices(result.memory.id)
                    return memory_manager.schedule_write_mark_due(result.memory, result.outcome)

            reconciliation_results = await asyncio.gather(
                *(reconcile(result) for result in outcomes)
            )
            mark_due_tasks = [task for task in reconciliation_results if task is not None]
            if mark_due_tasks:
                await asyncio.gather(*mark_due_tasks)
            return restored_count

        owned_task = asyncio.create_task(restore_owned(), name="memory-backup-restore")
        try:
            return await asyncio.shield(owned_task)
        except asyncio.CancelledError:
            while not owned_task.done():
                try:
                    await asyncio.shield(owned_task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
            with suppress(BaseException):
                owned_task.result()
            raise
        except MemoryRestoreError:
            raise
        except Exception as exc:
            logger.exception("Failed to restore memories: %s", exc)
            raise MemoryRestoreError(f"Failed to restore memories: {exc}") from exc

    def restore_sync(self) -> int:
        """Explicitly restore the configured backup path."""
        if not self.config.enabled or self.memory_manager is None:
            return 0
        return asyncio.run(self.restore())

    def _restore_memories_sync(self, path: Path) -> int:
        restored_count, _outcomes = self._restore_memories_with_outcomes_sync(path)
        return restored_count

    def _restore_memories_with_outcomes_sync(
        self,
        path: Path,
    ) -> tuple[int, list[MemoryWriteResult[Memory]]]:
        memory_manager = self.memory_manager
        if memory_manager is None:
            return 0, []

        records = _load_memory_backup(path)
        restored_count = 0
        outcomes: list[MemoryWriteResult[Memory]] = []
        with self.db.transaction() as conn:
            existing_session_ids = {
                row["id"] for row in conn.execute("SELECT id FROM sessions").fetchall()
            }
            existing_task_ids = {
                str(row["id"]) for row in conn.execute("SELECT id FROM tasks").fetchall()
            }
            existing_project_ids = {
                row["id"] for row in conn.execute("SELECT id FROM projects").fetchall()
            }
            for record in records:
                if record["project_id"] not in existing_project_ids:
                    raise MemoryRestoreError(
                        f"Memory {record['id']}: owner project {record['project_id']} does not exist"
                    )
                existing = conn.execute(
                    "SELECT updated_at FROM memories WHERE id = %s",
                    (record["id"],),
                ).fetchone()
                if existing is not None:
                    existing_updated_at = parse_stored_datetime(existing["updated_at"])
                    if (
                        existing_updated_at is not None
                        and record["updated_at"] <= existing_updated_at
                    ):
                        continue
                source_session_id = record.get("source_id")
                if source_session_id not in existing_session_ids:
                    source_session_id = None
                source_task_id = record.get("source_task_id")
                if source_task_id is not None:
                    source_task_id = str(source_task_id)
                if source_task_id not in existing_task_ids:
                    source_task_id = None
                result = memory_manager.storage.create_memory_with_outcome(
                    content=record["content"],
                    memory_type=record["type"],
                    project_id=record["project_id"],
                    is_global=record["is_global"],
                    tags=record.get("tags") or [],
                    source_type=record.get("source", "agent"),
                    source_session_id=source_session_id,
                    memory_id=record["id"],
                    created_at=record["created_at"],
                    updated_at=record["updated_at"],
                    rationale=record.get("rationale"),
                    source_task_id=source_task_id,
                    created_by_agent=record.get("created_by_agent"),
                )
                restored_count += 1
                if result.outcome in {"created", "reactivated", "updated"}:
                    outcomes.append(result)

        return restored_count, outcomes

    def _backup_memories_sync(self, path: Path, project_id: str | None = None) -> int:
        memory_manager = self.memory_manager
        if memory_manager is None:
            return 0

        memories: list[Any] = []
        page_size = 1000
        offset = 0
        while True:
            scope = MemoryScope.owner(project_id) if project_id is not None else ALL_MEMORIES
            page = memory_manager.storage.list_memories(
                scope=scope,
                limit=page_size,
                offset=offset,
            )
            memories.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

        records = sorted(
            (
                {
                    "id": memory.id,
                    "content": memory.content,
                    "type": memory.memory_type.value,
                    "tags": memory.tags,
                    "created_at": memory.created_at,
                    "updated_at": memory.updated_at,
                    "source": memory.source_type,
                    "source_id": memory.source_session_id,
                    "rationale": memory.rationale,
                    "source_task_id": memory.source_task_id,
                    "created_by_agent": memory.created_by_agent,
                    "project_id": memory.project_id,
                    "is_global": memory.is_global,
                }
                for memory in memories
            ),
            key=lambda record: record["id"],
        )
        content = "".join(
            json_dumps(
                _jsonl_memory_record(record),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
            for record in records
        )

        try:
            with export_file_lock(path):
                atomic_write_text(path, content)
        except Exception as exc:
            logger.exception("Failed to write memory backup: %s", exc)
            raise MemoryBackupError(f"Failed to write memory backup: {exc}") from exc

        return len(records)
