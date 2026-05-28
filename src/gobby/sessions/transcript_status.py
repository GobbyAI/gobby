"""Transcript availability/status assembly."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.sessions.transcript_archive import get_archive_dir
from gobby.sessions.transcript_io import (
    _count_nonempty_lines,
    _decompress_archive,
    _read_json_file,
    _read_jsonl_lines,
)
from gobby.sessions.transcript_parsing import _parse_json_session, _parse_lines
from gobby.sessions.transcript_paths import _is_json_session_file
from gobby.sessions.transcript_source import _resolve_effective_source

if TYPE_CHECKING:
    from gobby.sessions.transcripts.base import ParsedMessage
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


async def get_transcript_status_for_session(
    *,
    session_manager: SessionManager,
    archive_dir: str | None,
    session_id: str,
    get_live_transcript_path: Callable[[str, Session], Awaitable[str | None]],
    get_parsed_messages_from_archive: Callable[[str], Awaitable[list[ParsedMessage]]],
) -> dict[str, Any]:
    """Report transcript availability and parseability for a session."""
    session = session_manager.get(session_id)
    if not session:
        return {
            "session_id": session_id,
            "live_exists": False,
            "archive_exists": False,
            "availability": "missing",
            "content_state": "missing",
            "session_source": None,
            "detected_source": None,
            "source_mismatch": False,
            "raw_record_count": 0,
            "parsed_message_count": 0,
        }

    transcript_path = await get_live_transcript_path(session_id, session)
    live_exists = bool(transcript_path and os.path.isfile(transcript_path))

    archive_exists = False
    archive_path: Path | None = None
    if session.external_id:
        archive_path = get_archive_dir(archive_dir) / f"{session.external_id}.jsonl.gz"
        archive_exists = archive_path.is_file()

    raw_record_count = 0
    parsed_message_count = 0
    detected_source: str | None = None
    parse_failed = False

    if live_exists and transcript_path:
        if _is_json_session_file(transcript_path):
            try:
                data = await asyncio.to_thread(_read_json_file, transcript_path)
                raw_record_count = len(data.get("messages", [])) if isinstance(data, dict) else 0
                effective_source, detected_source = _resolve_effective_source(
                    session,
                    transcript_path=transcript_path,
                    data=data,
                    session_id=session_id,
                )
                parsed_message_count = len(
                    _parse_json_session(
                        data,
                        effective_source,
                        session_id=session_id,
                        transcript_path=transcript_path,
                    )
                )
            except (json.JSONDecodeError, ValueError, OSError) as e:
                logger.warning(f"Failed to parse JSON transcript for session {session_id}: {e}")
                parse_failed = True
        else:
            try:
                lines = await asyncio.to_thread(_read_jsonl_lines, transcript_path)
                raw_record_count = _count_nonempty_lines(lines)
                effective_source, detected_source = _resolve_effective_source(
                    session,
                    transcript_path=transcript_path,
                    lines=lines,
                    session_id=session_id,
                )
                parsed_message_count = len(
                    _parse_lines(
                        lines,
                        effective_source,
                        session_id=session_id,
                        transcript_path=transcript_path,
                    )
                )
            except (json.JSONDecodeError, ValueError, OSError) as e:
                logger.warning(f"Failed to parse JSONL transcript for session {session_id}: {e}")
                parse_failed = True
    elif archive_exists and archive_path is not None:
        try:
            lines = await asyncio.to_thread(_decompress_archive, str(archive_path))
            raw_record_count = _count_nonempty_lines(lines)
            _, detected_source = _resolve_effective_source(
                session,
                transcript_path=None,
                lines=lines,
                session_id=session_id,
            )
            parsed_message_count = len(await get_parsed_messages_from_archive(session_id))
        except (json.JSONDecodeError, ValueError, OSError, gzip.BadGzipFile) as e:
            logger.warning(f"Failed to read archive for session {session_id}: {e}")
            parse_failed = True

    availability = "missing"
    if live_exists:
        availability = "live"
    elif archive_exists:
        availability = "archive_only"

    content_state = "missing"
    if availability != "missing":
        if parse_failed:
            content_state = "unparseable"
        elif parsed_message_count > 0:
            content_state = "messages"
        elif raw_record_count > 0 and detected_source is None:
            content_state = "unparseable"
        else:
            content_state = "empty"

    session_source = getattr(session, "source", None)
    return {
        "session_id": session_id,
        "live_exists": live_exists,
        "archive_exists": archive_exists,
        "availability": availability,
        "content_state": content_state,
        "session_source": session_source,
        "detected_source": detected_source,
        "source_mismatch": bool(
            detected_source and session_source and detected_source != session_source
        ),
        "raw_record_count": raw_record_count,
        "parsed_message_count": parsed_message_count,
    }
