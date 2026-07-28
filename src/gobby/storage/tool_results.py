"""Persistence for oversized MCP tool results."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, TypedDict

from gobby.config.features import ToolResultOffloadConfig
from gobby.storage.hub.protocol import HubDatabase


class ToolResultSlice(TypedDict):
    """A bounded page from stored tool-result content."""

    content: str
    offset: int
    next_offset: int | None
    total_chars: int
    stored_chars: int


class ToolResultMeta(TypedDict):
    """Metadata exposed after project and retention checks."""

    result_id: str
    project_id: str
    session_id: str | None
    server_name: str
    tool_name: str
    content_kind: str
    total_chars: int
    stored_chars: int
    created_at: datetime


class ToolResultStore:
    """Store and retrieve project-owned oversized tool results."""

    def __init__(self, db: HubDatabase, config: ToolResultOffloadConfig) -> None:
        self._db = db
        self._config = config

    def save(
        self,
        *,
        project_id: str,
        session_id: str | None,
        server_name: str,
        tool_name: str,
        content: str,
        content_kind: Literal["json", "text"],
        total_chars: int,
    ) -> str:
        """Persist content and its bounded search chunks."""
        result_id = str(uuid.uuid4())
        chunks = _chunk_content(content, self._config.chunk_chars)
        chunk_rows = [
            (
                str(uuid.uuid4()),
                result_id,
                ordinal,
                start_offset,
                end_offset,
                chunk,
            )
            for ordinal, (start_offset, end_offset, chunk) in enumerate(chunks)
        ]

        with self._db.transaction() as transaction:
            transaction.execute(
                """INSERT INTO tool_results (
                       id, project_id, session_id, server_name, tool_name,
                       content, content_kind, total_chars, stored_chars
                   )
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    result_id,
                    project_id,
                    session_id,
                    server_name,
                    tool_name,
                    content,
                    content_kind,
                    total_chars,
                    len(content),
                ),
            )
            if chunk_rows:
                transaction.executemany(
                    """INSERT INTO tool_result_chunks (
                           id, result_id, ordinal, start_offset, end_offset, content
                       )
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    chunk_rows,
                )
        return result_id

    def cleanup_expired(self) -> int:
        """Delete retained results that have exceeded the configured lifetime."""
        cursor = self._db.execute(
            """DELETE FROM tool_results
               WHERE created_at < NOW() - make_interval(days => %s)""",
            (self._config.retention_days,),
        )
        return max(cursor.rowcount, 0)

    def get_slice(
        self,
        result_id: str,
        project_id: str,
        offset: int,
        limit: int,
    ) -> ToolResultSlice | None:
        """Return a bounded page when the result is visible and unexpired."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if limit <= 0:
            raise ValueError("limit must be positive")

        canonical_id = _parse_result_id(result_id)
        if canonical_id is None:
            return None

        row = self._db.fetchone(
            """SELECT SUBSTRING(content FROM %s FOR %s) AS content,
                      total_chars, stored_chars
               FROM tool_results
               WHERE id = %s
                 AND project_id = %s
                 AND created_at >= NOW() - make_interval(days => %s)""",
            (
                offset + 1,
                limit,
                canonical_id,
                project_id,
                self._config.retention_days,
            ),
        )
        if row is None:
            return None

        page = str(row["content"])
        stored_chars = int(row["stored_chars"])
        page_end = offset + len(page)
        return {
            "content": page,
            "offset": offset,
            "next_offset": page_end if page_end < stored_chars else None,
            "total_chars": int(row["total_chars"]),
            "stored_chars": stored_chars,
        }

    def get_meta(self, result_id: str, project_id: str) -> ToolResultMeta | None:
        """Return metadata when the result is visible and unexpired."""
        canonical_id = _parse_result_id(result_id)
        if canonical_id is None:
            return None

        row = self._db.fetchone(
            """SELECT id, project_id, session_id, server_name, tool_name,
                      content_kind, total_chars, stored_chars, created_at
               FROM tool_results
               WHERE id = %s
                 AND project_id = %s
                 AND created_at >= NOW() - make_interval(days => %s)""",
            (canonical_id, project_id, self._config.retention_days),
        )
        if row is None:
            return None
        return {
            "result_id": str(row["id"]),
            "project_id": str(row["project_id"]),
            "session_id": str(row["session_id"]) if row["session_id"] is not None else None,
            "server_name": str(row["server_name"]),
            "tool_name": str(row["tool_name"]),
            "content_kind": str(row["content_kind"]),
            "total_chars": int(row["total_chars"]),
            "stored_chars": int(row["stored_chars"]),
            "created_at": row["created_at"],
        }


def _parse_result_id(result_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(result_id)
    except (AttributeError, ValueError):
        return None


def _chunk_content(content: str, chunk_chars: int) -> list[tuple[int, int, str]]:
    """Split content on line boundaries, hard-splitting oversized lines."""
    chunks: list[tuple[int, int, str]] = []
    buffered = ""
    buffered_start = 0

    def flush_buffer() -> None:
        nonlocal buffered, buffered_start
        if not buffered:
            return
        end_offset = buffered_start + len(buffered)
        chunks.append((buffered_start, end_offset, buffered))
        buffered = ""
        buffered_start = end_offset

    for line in content.splitlines(keepends=True):
        if len(line) <= chunk_chars:
            if buffered and len(buffered) + len(line) > chunk_chars:
                flush_buffer()
            buffered += line
            continue

        flush_buffer()
        line_start = buffered_start
        full_chunks_end = len(line) - (len(line) % chunk_chars)
        for relative_start in range(0, full_chunks_end, chunk_chars):
            relative_end = relative_start + chunk_chars
            chunks.append(
                (
                    line_start + relative_start,
                    line_start + relative_end,
                    line[relative_start:relative_end],
                )
            )
        buffered_start = line_start + full_chunks_end
        buffered = line[full_chunks_end:]

    flush_buffer()
    return chunks
