"""Unified transcript read layer: live transcript file -> gzip archive.

Reads from live transcript files for active/paused sessions and falls back to
gzip archives for expired sessions. Supports JSONL and native JSON transcripts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import warnings
from typing import TYPE_CHECKING, Any

from gobby.sessions.transcript_archive import get_archive_dir
from gobby.sessions.transcript_io import (
    DecompressionError,
    _decompress_archive,
    _read_json_file,
    _read_jsonl_lines,
    clear_archive_cache,
)
from gobby.sessions.transcript_parsing import (
    _parse_json_session,
    _parse_lines,
    _parse_lines_to_dicts,
    _parsed_to_dicts,
)
from gobby.sessions.transcript_paths import _find_transcript_on_disk, _is_json_session_file
from gobby.sessions.transcript_renderer import render_transcript
from gobby.sessions.transcript_source import _resolve_effective_source
from gobby.sessions.transcript_status import get_transcript_status_for_session

if TYPE_CHECKING:
    from gobby.sessions.transcript_renderer import RenderedMessage
    from gobby.sessions.transcripts.base import ParsedMessage
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

__all__ = ["TranscriptReader", "clear_archive_cache"]


class TranscriptReader:
    """Unified read layer: live transcript first, gzip archive fallback."""

    def __init__(
        self,
        session_manager: SessionManager,
        archive_dir: str | None = None,
        # Deprecated: kept for backwards-compat callers, ignored
        message_manager: object | None = None,
    ):
        if message_manager is not None:
            warnings.warn(
                "message_manager is deprecated and ignored",
                DeprecationWarning,
                stacklevel=2,
            )
        self._session_manager = session_manager
        self._archive_dir = archive_dir

    async def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get messages for a session, falling back to gzip archive."""
        has_live_transcript = await self._has_live_transcript(session_id)
        file_messages = await self._read_from_file(session_id, limit, offset, role)
        if has_live_transcript:
            return file_messages

        return await self._read_from_archive(session_id, limit, offset, role)

    async def get_rendered_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RenderedMessage]:
        """Get grouped, rendered messages for a session."""
        has_live_transcript = await self._has_live_transcript(session_id)
        parsed_messages = await self._get_parsed_messages_from_file(session_id)

        if not has_live_transcript:
            parsed_messages = await self._get_parsed_messages_from_archive(session_id)

        if not parsed_messages:
            return []

        rendered = render_transcript(parsed_messages, session_id=session_id)
        return rendered[offset : offset + limit]

    async def count_messages(self, session_id: str) -> int:
        """Count messages for a session from live transcript or gzip archive."""
        session = self._session_manager.get(session_id)
        if not session:
            return 0

        if await self._has_live_transcript(session_id):
            return len(await self._get_parsed_messages_from_file(session_id))

        return len(await self._get_parsed_messages_from_archive(session_id))

    async def get_transcript_status(self, session_id: str) -> dict[str, Any]:
        """Report transcript availability and parseability for a session."""
        return await get_transcript_status_for_session(
            session_manager=self._session_manager,
            archive_dir=self._archive_dir,
            session_id=session_id,
            get_live_transcript_path=self._get_live_transcript_path,
            get_parsed_messages_from_archive=self._get_parsed_messages_from_archive,
        )

    async def _get_parsed_messages_from_archive(self, session_id: str) -> list[ParsedMessage]:
        """Read and parse ParsedMessages from gzip archive."""
        session = self._session_manager.get(session_id)
        if not session or not session.external_id:
            return []

        archive_path = get_archive_dir(self._archive_dir) / f"{session.external_id}.jsonl.gz"
        if not archive_path.is_file():
            return []

        try:
            lines = list(await asyncio.to_thread(_decompress_archive, str(archive_path)))
            source, _ = _resolve_effective_source(
                session,
                transcript_path=None,
                lines=lines,
                session_id=session_id,
            )
            return _parse_lines(
                lines,
                source,
                session_id=session_id,
                transcript_path=archive_path,
            )
        except (DecompressionError, json.JSONDecodeError, ValueError, OSError) as e:
            logger.warning(f"Failed to read archive for session {session_id}: {e}")
            return []

    async def _get_live_transcript_path(self, session_id: str, session: Session) -> str | None:
        """Resolve a usable live transcript path for a session."""
        transcript_path = getattr(session, "transcript_path", None)
        source = session.source or "claude"
        return await self._ensure_transcript_path(session_id, session, source, transcript_path)

    async def _has_live_transcript(self, session_id: str) -> bool:
        """Return True when a live transcript file exists for the session."""
        session = self._session_manager.get(session_id)
        if not session:
            return False

        transcript_path = await self._get_live_transcript_path(session_id, session)
        return bool(transcript_path and os.path.isfile(transcript_path))

    async def _ensure_transcript_path(
        self,
        session_id: str,
        session: Session,
        source: str,
        transcript_path: str | None,
    ) -> str | None:
        """Return a valid transcript path, re-deriving and persisting it when needed."""
        if (
            transcript_path
            and transcript_path != "missing_transcript"
            and os.path.isfile(transcript_path)
        ):
            return transcript_path

        external_id = getattr(session, "external_id", None)
        derived = await asyncio.to_thread(_find_transcript_on_disk, source, external_id or "")
        if not derived:
            return None

        try:
            await asyncio.to_thread(
                self._session_manager.update, session_id, transcript_path=derived
            )
            logger.info(f"Re-derived transcript path for session {session_id}: {derived}")
        except (OSError, ValueError) as e:
            logger.warning(
                f"Failed to persist re-derived transcript path for session {session_id} "
                f"({derived}): {e}"
            )
        return derived

    async def _get_parsed_messages_from_file(self, session_id: str) -> list[ParsedMessage]:
        """Read and parse ParsedMessages from live transcript file."""
        session = self._session_manager.get(session_id)
        if not session:
            return []

        transcript_path = await self._get_live_transcript_path(session_id, session)
        if not transcript_path:
            return []

        try:
            if _is_json_session_file(transcript_path):
                data = await asyncio.to_thread(self._read_json_file, transcript_path)
                source, _ = _resolve_effective_source(
                    session,
                    transcript_path=transcript_path,
                    data=data,
                    session_id=session_id,
                )
                return _parse_json_session(
                    data,
                    source,
                    session_id=session_id,
                    transcript_path=transcript_path,
                )

            lines = await asyncio.to_thread(self._read_jsonl_lines, transcript_path)
            source, _ = _resolve_effective_source(
                session,
                transcript_path=transcript_path,
                lines=lines,
                session_id=session_id,
            )
            return _parse_lines(
                lines,
                source,
                session_id=session_id,
                transcript_path=transcript_path,
            )
        except Exception as e:
            logger.warning(f"Failed to read transcript for session {session_id}: {e}")
            return []

    async def _read_from_archive(
        self,
        session_id: str,
        limit: int,
        offset: int,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read messages from gzip archive for a session."""
        session = self._session_manager.get(session_id)
        if not session or not session.external_id:
            return []

        archive_path = get_archive_dir(self._archive_dir) / f"{session.external_id}.jsonl.gz"
        if not archive_path.is_file():
            return []

        try:
            lines = list(await asyncio.to_thread(_decompress_archive, str(archive_path)))
            source, _ = _resolve_effective_source(
                session,
                transcript_path=None,
                lines=lines,
                session_id=session_id,
            )
            all_messages = _parse_lines_to_dicts(
                lines,
                source,
                session_id=session_id,
                transcript_path=archive_path,
            )
        except (DecompressionError, json.JSONDecodeError, ValueError, OSError) as e:
            logger.warning(f"Failed to read archive for session {session_id}: {e}")
            return []

        return _filter_messages(all_messages, session_id=session_id, role=role)[
            offset : offset + limit
        ]

    async def _read_from_file(
        self,
        session_id: str,
        limit: int,
        offset: int,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read messages from a live transcript file on disk."""
        session = self._session_manager.get(session_id)
        if not session:
            return []

        transcript_path = await self._get_live_transcript_path(session_id, session)
        if not transcript_path:
            return []

        try:
            if _is_json_session_file(transcript_path):
                data = await asyncio.to_thread(self._read_json_file, transcript_path)
                source, _ = _resolve_effective_source(
                    session,
                    transcript_path=transcript_path,
                    data=data,
                    session_id=session_id,
                )
                parsed = _parse_json_session(
                    data,
                    source,
                    session_id=session_id,
                    transcript_path=transcript_path,
                )
                all_messages = _parsed_to_dicts(parsed)
            else:
                lines = await asyncio.to_thread(self._read_jsonl_lines, transcript_path)
                source, _ = _resolve_effective_source(
                    session,
                    transcript_path=transcript_path,
                    lines=lines,
                    session_id=session_id,
                )
                all_messages = _parse_lines_to_dicts(
                    lines,
                    source,
                    session_id=session_id,
                    transcript_path=transcript_path,
                )
        except Exception as e:
            logger.warning(f"Failed to read transcript for session {session_id}: {e}")
            return []

        return _filter_messages(all_messages, session_id=session_id, role=role)[
            offset : offset + limit
        ]

    @staticmethod
    def _read_jsonl_lines(path: str) -> list[str]:
        """Read lines from a JSONL file. Runs in a thread."""
        return _read_jsonl_lines(path)

    @staticmethod
    def _read_json_file(path: str) -> dict[str, Any]:
        """Read and parse a JSON file. Runs in a thread."""
        return _read_json_file(path)


def _filter_messages(
    messages: list[dict[str, Any]],
    *,
    session_id: str,
    role: str | None,
) -> list[dict[str, Any]]:
    """Attach session ID and apply optional role filtering."""
    normalized = [{**msg, "session_id": session_id} for msg in messages]

    if role:
        return [msg for msg in normalized if msg.get("role") == role]
    return normalized
