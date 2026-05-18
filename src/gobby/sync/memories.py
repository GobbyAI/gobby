"""Memory backup utilities for filesystem export.

This module provides JSONL backup functionality for memories. It is NOT a
bidirectional sync mechanism - memories are stored in the database via
MemoryBackendProtocol. This module handles:

- Backup export to .gobby/memories.jsonl for disaster recovery
- Explicit restore/import from existing JSONL files
- On-demand backup via CLI, pre-push hook, and MCP export

Classes:
    MemoryBackupManager: Main backup manager (formerly MemorySyncManager)
    MemorySyncManager: Backward-compatible alias for MemoryBackupManager
"""

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import string
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "MemoryBackupManager",
    "MemorySyncManager",  # Backward compatibility alias
]

from gobby.config.persistence import MemoryBackupConfig
from gobby.memory.manager import MemoryManager
from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


def _parse_updated_at(value: Any) -> tuple[int, str]:
    """Build a sortable timestamp key for memory export/import deduplication."""
    if isinstance(value, str) and value:
        try:
            return (1, datetime.fromisoformat(value).isoformat())
        except ValueError:
            return (0, value)
    return (0, "")


def _parse_created_at(value: Any) -> tuple[int, str]:
    """Build a sortable created_at key that treats missing values as newest."""
    if isinstance(value, str) and value:
        try:
            return (0, datetime.fromisoformat(value).isoformat())
        except ValueError:
            return (0, value)
    return (1, "")


_MEMORY_NORMALIZE_PUNCTUATION = str.maketrans("", "", string.punctuation)
_MEMORY_NORMALIZE_WHITESPACE_RE = re.compile(r"\s+")
_MEMORY_FUZZY_MIN_LENGTH = 160
_MEMORY_FUZZY_DUPLICATE_THRESHOLD = 0.96
_MEMORY_FUZZY_PREFIX_LENGTH = 96
_MEMORY_TYPE_RANK = {
    "fact": 10,
    "context": 20,
    "pattern": 30,
    "preference": 40,
    "decision": 50,
    "codebase_fact": 60,
    "codebase_decision": 70,
    "implementation_summary": 80,
}


def _normalized_memory_content(content: Any) -> str:
    """Normalize content for conservative cross-machine backup deduplication."""
    text = str(content or "").casefold()
    text = text.translate(_MEMORY_NORMALIZE_PUNCTUATION)
    return _MEMORY_NORMALIZE_WHITESPACE_RE.sub(" ", text).strip()


def _normalized_memory_hash(content: Any) -> str:
    normalized = _normalized_memory_content(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_fuzzy_memory_duplicate(left: str, right: str) -> bool:
    if min(len(left), len(right)) < _MEMORY_FUZZY_MIN_LENGTH:
        return False
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio() >= (
        _MEMORY_FUZZY_DUPLICATE_THRESHOLD
    )


def _memory_fuzzy_bucket_key(normalized_content: str) -> str:
    return normalized_content[:_MEMORY_FUZZY_PREFIX_LENGTH]


def _find_fuzzy_duplicate(
    normalized: str,
    *,
    fuzzy_buckets: dict[str, list[str]],
    normalized_by_key: dict[str, str],
) -> str | None:
    """Find an existing near-duplicate record key for normalized content."""
    fuzzy_bucket = fuzzy_buckets.get(_memory_fuzzy_bucket_key(normalized), [])
    for key in fuzzy_bucket:
        existing_normalized = normalized_by_key.get(key)
        if existing_normalized is None:
            continue
        if _is_fuzzy_memory_duplicate(normalized, existing_normalized):
            return key
    return None


def _memory_type_rank(record: Mapping[str, Any]) -> int:
    memory_type = str(record.get("type") or record.get("memory_type") or "fact").lower()
    return _MEMORY_TYPE_RANK.get(memory_type, 0)


def _record_tags(record: Mapping[str, Any]) -> list[str]:
    raw_tags = record.get("tags")
    if not isinstance(raw_tags, list):
        return []
    return [str(tag).strip() for tag in raw_tags if str(tag).strip()]


def _merge_memory_records(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicate JSONL records while preserving durable provenance fields."""
    latest = (
        candidate
        if _parse_updated_at(candidate.get("updated_at"))
        >= _parse_updated_at(existing.get("updated_at"))
        else existing
    )
    earliest = (
        candidate
        if _parse_created_at(candidate.get("created_at"))
        < _parse_created_at(existing.get("created_at"))
        else existing
    )
    specific = candidate if _memory_type_rank(candidate) > _memory_type_rank(existing) else existing

    merged = dict(latest)
    merged["created_at"] = earliest.get("created_at", merged.get("created_at"))
    merged["updated_at"] = latest.get("updated_at", merged.get("updated_at"))
    merged["tags"] = sorted(set(_record_tags(existing)) | set(_record_tags(candidate)))
    merged["type"] = (
        specific.get("type") or specific.get("memory_type") or merged.get("type", "fact")
    )
    return merged


_EPHEMERAL_IMPLEMENTATION_TAGS = {"build-e2e"}
_EPHEMERAL_CONTENT_MARKERS = ("build #epic", "docs test #", "merged into local")


def is_ephemeral_implementation_note(record: Mapping[str, Any]) -> bool:
    """Return True for run-specific implementation notes that should not persist."""
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


class MemoryBackupManager:
    """
    Manages backup of memories from the database to filesystem.

    This is a backup/export utility, NOT a sync mechanism. Memories are stored
    in the database (via the configured backend) and this class provides:
    - JSONL backup export (to .gobby/memories.jsonl)
    - Explicit restore/import from existing JSONL files
    - On-demand backup via CLI, pre-push hook, and MCP export

    For actual memory storage, see gobby.memory.backends.
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        memory_manager: MemoryManager | None,
        config: MemoryBackupConfig,
    ):
        self.db = db
        self.memory_manager = memory_manager
        self.config = config
        self.export_path = config.export_path

    def _get_export_path(self) -> Path:
        """Get the path for the memories.jsonl file.

        Returns the export_path, resolving relative paths against the project context.
        """
        if self.export_path.is_absolute():
            return self.export_path

        # Try to get project path from project context
        try:
            from gobby.utils.project_context import get_project_context

            project_ctx = get_project_context()
            if project_ctx and project_ctx.get("project_path"):
                project_path = Path(project_ctx["project_path"]).expanduser().resolve()
                return project_path / self.export_path
        except Exception as e:
            logger.debug(f"Fallback to cwd since project context unavailable: {e}")

        # Fall back to current working directory
        return Path.cwd() / self.export_path

    async def import_from_files(self) -> int:
        """
        Import memories from filesystem (one-time migration).

        This is intended for migrating existing JSONL backup files into the
        database. For ongoing memory storage, use the memory backend directly.

        Returns:
            Count of imported memories
        """
        if not self.config.enabled:
            return 0

        if not self.memory_manager:
            return 0

        memories_file = self._get_export_path()
        if not memories_file.exists():
            return 0

        return await asyncio.to_thread(self._import_memories_sync, memories_file)

    def backup_sync(self, project_id: str | None = None) -> int:
        """
        Backup memories to filesystem synchronously (blocking).

        Used to force a backup write before the async loop starts.
        This is a one-way export for backup purposes only.

        Args:
            project_id: Optional project to scope export to.
        """
        if not self.config.enabled:
            return 0

        if not self.memory_manager:
            return 0

        try:
            memories_file = self._get_export_path()
            return self._export_to_files_sync(memories_file, project_id=project_id)
        except Exception as e:
            logger.warning(f"Failed to backup memories: {e}")
            return 0

    # Backward compatibility alias
    export_sync = backup_sync

    def import_sync(self, force: bool = False) -> int:
        """
        Import memories from filesystem synchronously (blocking).

        Used by explicit restore/import commands to restore memories from a JSONL file
        (e.g. pulled from git on a new machine) before exporting.
        Only imports if the JSONL file has more entries than the DB.
        """
        if not self.config.enabled or not self.memory_manager:
            return 0

        try:
            memories_file = self._get_export_path()
            if not memories_file.exists():
                return 0

            # Read file once — used for both counting and importing
            with open(memories_file, encoding="utf-8") as f:
                lines = [line for line in f if line.strip()]

            file_count = len(lines)
            if file_count == 0:
                return 0

            # Count memories in DB
            db_count = self.memory_manager.count_memories()

            if not force and file_count <= db_count:
                logger.debug(
                    f"Skipping memory import: DB has {db_count} memories, file has {file_count}"
                )
                return 0

            logger.info(
                f"Importing memories from {memories_file}: file has {file_count}, DB has {db_count}"
            )
            return self._import_memories_from_lines(lines)
        except Exception as e:
            logger.warning(f"Failed to import memories: {e}")
            return 0

    async def export_to_files(self, project_id: str | None = None) -> int:
        """
        Backup memories to filesystem as JSONL.

        This exports all memories to a JSONL file for backup purposes.
        The file can be used for disaster recovery or migration.

        Args:
            project_id: Optional project to scope export to.

        Returns:
            Count of backed up memories
        """
        if not self.config.enabled:
            return 0

        if not self.memory_manager:
            return 0

        memories_file = self._get_export_path()
        return await asyncio.to_thread(
            self._export_to_files_sync, memories_file, project_id=project_id
        )

    def _export_to_files_sync(self, memories_file: Path, project_id: str | None = None) -> int:
        """Synchronous implementation of export."""
        return self._export_memories_sync(memories_file, project_id=project_id)

    def _import_memories_sync(self, file_path: Path) -> int:
        """Import memories from JSONL file (sync)."""
        if not self.memory_manager:
            return 0
        try:
            with open(file_path, encoding="utf-8") as f:
                lines = [line for line in f if line.strip()]
        except OSError as e:
            logger.warning(f"Failed to import memories: {e}")
            return 0
        return self._import_memories_from_lines(lines)

    def _import_memories_from_lines(self, lines: list[str]) -> int:
        """Import memories from pre-read JSONL lines."""
        if not self.memory_manager:
            return 0

        count = 0
        skipped = 0
        parsed_records: list[dict[str, Any]] = []
        try:
            for line_num, line in enumerate(lines, 1):
                try:
                    data = json.loads(line)

                    if not self._validate_memory_record(data, line_num):
                        skipped += 1
                        continue

                    content = data.get("content", "")
                    data["content"] = self._sanitize_content(content)
                    if is_ephemeral_implementation_note(data):
                        skipped += 1
                        continue
                    parsed_records.append(data)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON in memories file: {line[:50]}...")
                except Exception as e:
                    logger.debug(f"Skipping memory import: {e}")

        except Exception as e:
            logger.error(f"Failed to import memories: {e}")

        for data in self._deduplicate_records_by_id(parsed_records):
            content = data.get("content", "")
            if self.memory_manager.content_exists(content):
                skipped += 1
                continue

            raw_source = data.get("source", "agent")
            source_type = raw_source if raw_source in ("user", "agent") else "agent"
            try:
                self.memory_manager.storage.create_memory(
                    content=content,
                    memory_type=data.get("type", "fact"),
                    tags=data.get("tags", []),
                    source_type=source_type,
                )
                count += 1
            except Exception as e:
                logger.warning(f"Failed to import memory: {e}")
                continue

        if skipped > 0:
            logger.debug(f"Skipped {skipped} duplicate memories during import")

        return count

    def _validate_memory_record(self, data: dict[str, Any], line_num: int) -> bool:
        """Validate a memory record before import.

        Checks that required fields are present and well-formed. Auto-converts
        comma-delimited tag strings to lists.

        Args:
            data: Parsed JSON record from JSONL line.
            line_num: 1-based line number for log messages.

        Returns:
            True if valid (possibly after auto-fix), False if should be skipped.
        """
        # Verify content exists and is a non-empty string
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            logger.warning(f"Skipping memory at line {line_num}: missing or empty content")
            return False

        # Verify tags is a list; auto-convert comma-delimited strings
        tags = data.get("tags")
        if tags is not None and not isinstance(tags, list):
            if isinstance(tags, str):
                logger.warning(
                    f"Auto-converting comma-delimited tags string to list at line {line_num}",
                )
                data["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
            else:
                logger.warning(f"Skipping memory at line {line_num}: tags is not a list")
                return False

        return True

    def _deduplicate_records_by_id(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep one canonical record per id or conservatively equivalent content."""
        # Pass 1: dedup by ID
        canonical_by_key: dict[str, dict[str, Any]] = {}
        for record in records:
            record_id = str(record.get("id", "")).strip()
            key = record_id or record.get("content", "").strip()
            if not key:
                continue

            existing = canonical_by_key.get(key)
            if existing is None:
                canonical_by_key[key] = record
            else:
                canonical_by_key[key] = _merge_memory_records(existing, record)

        # Pass 2: dedup by normalized content, then fuzzy-match long near-duplicates.
        canonical_by_content: dict[str, dict[str, Any]] = {}
        normalized_by_key: dict[str, str] = {}
        fuzzy_buckets: dict[str, list[str]] = {}
        for record in canonical_by_key.values():
            content = record.get("content", "").strip()
            if not content:
                canonical_by_content[record.get("id", "")] = record
                continue

            normalized = _normalized_memory_content(content)
            if not normalized:
                continue
            content_key = _normalized_memory_hash(content)
            existing_key = content_key if content_key in canonical_by_content else None
            if existing_key is None:
                existing_key = _find_fuzzy_duplicate(
                    normalized,
                    fuzzy_buckets=fuzzy_buckets,
                    normalized_by_key=normalized_by_key,
                )
            if existing_key is None:
                canonical_by_content[content_key] = record
                normalized_by_key[content_key] = normalized
                fuzzy_buckets.setdefault(_memory_fuzzy_bucket_key(normalized), []).append(
                    content_key
                )
            else:
                canonical_by_content[existing_key] = _merge_memory_records(
                    canonical_by_content[existing_key],
                    record,
                )

        return list(canonical_by_content.values())

    def _sanitize_content(self, content: str) -> str:
        """Replace user home directories with ~ for privacy.

        Prevents absolute user paths like /Users/josh from being
        committed to version control. Also strips the project path
        prefix to produce project-relative paths.
        """
        home = os.path.expanduser("~")
        content = content.replace(home, "~")

        # Strip project path prefix to produce project-relative paths
        try:
            from gobby.utils.project_context import get_project_context

            project_ctx = get_project_context()
            if project_ctx and project_ctx.get("project_path"):
                repo_path = project_ctx["project_path"]
                # Normalize ~/Projects/foo/ form (after home replacement)
                tilde_path = repo_path.replace(home, "~")
                for prefix in (tilde_path + "/", tilde_path):
                    content = content.replace(prefix, "")
        except Exception as e:
            logger.debug(f"Best-effort sanitization failed: {e}")

        return content

    def _deduplicate_memories(self, memories: list[Any]) -> list[Any]:
        """Deduplicate memories by normalized content, keeping earliest.

        Args:
            memories: List of memory objects

        Returns:
            List of unique memories (by content), keeping the earliest created_at
        """
        seen_content: dict[str, Any] = {}  # normalized_content -> memory
        for memory in memories:
            normalized = memory.content.strip()
            if normalized not in seen_content:
                seen_content[normalized] = memory
            else:
                # Keep the one with earlier created_at
                existing = seen_content[normalized]
                if memory.created_at < existing.created_at:
                    seen_content[normalized] = memory
        return list(seen_content.values())

    def _export_memories_sync(self, file_path: Path, project_id: str | None = None) -> int:
        """Export memories to JSONL file (sync) with merge, deduplication, and path sanitization.

        Merges DB records with existing file records so that memories from other
        machines (pulled via git) are preserved. DB records are authoritative for
        shared content; file-only records survive untouched.

        Args:
            file_path: Target JSONL file path.
            project_id: Optional project to scope export to. When set, only
                memories for this project (plus global memories) are exported,
                and file records from other projects are dropped.
        """
        if not self.memory_manager:
            return 0

        try:
            # 1. Read existing file records (preserves records from other machines)
            existing_records: list[dict[str, Any]] = []
            if file_path.exists():
                try:
                    with open(file_path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                                if data.get("id") or data.get("content", "").strip():
                                    if not is_ephemeral_implementation_note(data):
                                        existing_records.append(data)
                            except json.JSONDecodeError as e:
                                logger.debug(f"Skipping malformed JSONL line in {file_path}: {e}")
                                continue
                except OSError as e:
                    logger.debug(f"Cannot read memories file {file_path}: {e}")

            # 2. Build DB records (authoritative for local content)
            memories: list[Any] = []
            page_size = 1000
            offset = 0
            while True:
                page = self.memory_manager.list_memories(
                    limit=page_size, offset=offset, project_id=project_id
                )
                memories.extend(page)
                if len(page) < page_size:
                    break
                offset += page_size
            unique_memories = self._deduplicate_memories(memories)

            db_records: list[dict[str, Any]] = []
            for memory in unique_memories:
                sanitized = self._sanitize_content(memory.content)
                record = {
                    "id": memory.id,
                    "content": sanitized,
                    "type": memory.memory_type,
                    "tags": memory.tags,
                    "created_at": memory.created_at,
                    "updated_at": memory.updated_at,
                    "source": memory.source_type,
                    "source_id": memory.source_session_id,
                    "project_id": memory.project_id,
                }
                if not is_ephemeral_implementation_note(record):
                    db_records.append(record)

            # 3. Merge: keep one canonical record per id, preferring the latest updated_at
            # When scoped to a project, drop file records that belong to
            # other projects. Preserve records without a project_id (from
            # other machines or legacy exports).
            if project_id:
                filtered_existing = [
                    record
                    for record in existing_records
                    if not record.get("project_id") or record.get("project_id") == project_id
                ]
            else:
                filtered_existing = existing_records
            sorted_records = sorted(
                self._deduplicate_records_by_id([*filtered_existing, *db_records]),
                key=lambda record: record.get("id") or record.get("content", ""),
            )

            # 4. Build output and skip write if content is unchanged
            new_content = "".join(
                json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n"
                for data in sorted_records
            )
            new_hash = hashlib.sha256(new_content.encode("utf-8")).digest()

            if file_path.exists():
                try:
                    existing_hash = hashlib.sha256(file_path.read_bytes()).digest()
                    if new_hash == existing_hash:
                        logger.debug("Memory export unchanged, skipping write")
                        return len(sorted_records)
                except OSError:
                    pass  # File unreadable — overwrite it

            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(new_content, encoding="utf-8")

            return len(sorted_records)
        except Exception as e:
            logger.error(f"Failed to export memories: {e}", exc_info=True)
            return 0


# Backward compatibility alias
MemorySyncManager = MemoryBackupManager
