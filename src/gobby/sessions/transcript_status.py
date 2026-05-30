"""Transcript availability/status assembly (index-backed counts).

Counts come from the cached boundary index (shared with the windowed message
path), so a status poll on a very large session does not trigger a second full
parse. The *detected* (nullable) source is still resolved from a bounded prefix
sample so ``source_mismatch`` / ``content_state`` semantics are preserved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.sessions.transcript_archive import get_archive_dir
from gobby.sessions.transcript_index import SOURCE_SAMPLE_LINES, get_or_build_index
from gobby.sessions.transcript_io import (
    DecompressionError,
    _iter_jsonl_lines,
    _read_archive_lines,
    _read_json_file,
)
from gobby.sessions.transcript_parsing import _parse_json_session
from gobby.sessions.transcript_paths import _is_json_session_file
from gobby.sessions.transcript_source import _resolve_effective_source

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def _missing_status(session_id: str) -> dict[str, Any]:
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


def _read_sample_lines(path: str, max_lines: int) -> list[str]:
    """Read a bounded prefix of JSONL lines for content source detection."""
    out: list[str] = []
    for line in _iter_jsonl_lines(path):
        out.append(line)
        if len(out) >= max_lines:
            break
    return out


async def _json_transcript_counts(
    session: Session,
    session_id: str,
    transcript_path: str,
) -> tuple[int, int, str | None, bool]:
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
        return raw_record_count, parsed_message_count, detected_source, False
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning(f"Failed to parse JSON transcript for session {session_id}: {e}")
        return 0, 0, None, True


async def _jsonl_transcript_counts(
    session: Session,
    session_id: str,
    transcript_path: str,
) -> tuple[int, int, str | None, bool]:
    try:
        st = await asyncio.to_thread(os.stat, transcript_path)
        sample = await asyncio.to_thread(_read_sample_lines, transcript_path, SOURCE_SAMPLE_LINES)
        effective_source, detected_source = _resolve_effective_source(
            session,
            transcript_path=transcript_path,
            lines=sample,
            session_id=session_id,
        )
        index = await get_or_build_index(
            transcript_path,
            effective_source,
            session_id,
            seek_mode="byte",
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
        )
        return index.raw_record_count, index.parsed_message_count, detected_source, False
    except (OSError, ValueError) as e:
        logger.warning(f"Failed to parse JSONL transcript for session {session_id}: {e}")
        return 0, 0, None, True


async def _archive_transcript_counts(
    session: Session,
    session_id: str,
    archive_path: Path,
) -> tuple[int, int, str | None, bool]:
    try:
        st = await asyncio.to_thread(os.stat, str(archive_path))
        lines = await asyncio.to_thread(_read_archive_lines, str(archive_path))
        effective_source, detected_source = _resolve_effective_source(
            session,
            transcript_path=None,
            lines=lines[:SOURCE_SAMPLE_LINES],
            session_id=session_id,
        )
        index = await get_or_build_index(
            str(archive_path),
            effective_source,
            session_id,
            seek_mode="line",
            lines=lines,
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
        )
        return index.raw_record_count, index.parsed_message_count, detected_source, False
    except (DecompressionError, json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning(f"Failed to read archive for session {session_id}: {e}")
        return 0, 0, None, True


def _availability(live_exists: bool, archive_exists: bool) -> str:
    if live_exists:
        return "live"
    if archive_exists:
        return "archive_only"
    return "missing"


def _content_state(
    availability: str,
    *,
    parse_failed: bool,
    parsed_message_count: int,
    raw_record_count: int,
    detected_source: str | None,
) -> str:
    if availability == "missing":
        return "missing"
    if parse_failed:
        return "unparseable"
    if parsed_message_count > 0:
        return "messages"
    if raw_record_count > 0 and detected_source is None:
        return "unparseable"
    return "empty"


def _final_status(
    *,
    session_id: str,
    session: Session,
    live_exists: bool,
    archive_exists: bool,
    raw_record_count: int,
    parsed_message_count: int,
    detected_source: str | None,
    parse_failed: bool,
) -> dict[str, Any]:
    availability = _availability(live_exists, archive_exists)
    session_source = getattr(session, "source", None)
    return {
        "session_id": session_id,
        "live_exists": live_exists,
        "archive_exists": archive_exists,
        "availability": availability,
        "content_state": _content_state(
            availability,
            parse_failed=parse_failed,
            parsed_message_count=parsed_message_count,
            raw_record_count=raw_record_count,
            detected_source=detected_source,
        ),
        "session_source": session_source,
        "detected_source": detected_source,
        "source_mismatch": bool(
            detected_source and session_source and detected_source != session_source
        ),
        "raw_record_count": raw_record_count,
        "parsed_message_count": parsed_message_count,
    }


async def get_transcript_status_for_session(
    *,
    session_manager: SessionManager,
    archive_dir: str | None,
    session_id: str,
    get_live_transcript_path: Callable[[str, Session], Awaitable[str | None]],
) -> dict[str, Any]:
    """Report transcript availability and parseability for a session."""
    session = session_manager.get(session_id)
    if not session:
        return _missing_status(session_id)

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
            (
                raw_record_count,
                parsed_message_count,
                detected_source,
                parse_failed,
            ) = await _json_transcript_counts(session, session_id, transcript_path)
        else:
            (
                raw_record_count,
                parsed_message_count,
                detected_source,
                parse_failed,
            ) = await _jsonl_transcript_counts(session, session_id, transcript_path)
    elif archive_exists and archive_path is not None:
        (
            raw_record_count,
            parsed_message_count,
            detected_source,
            parse_failed,
        ) = await _archive_transcript_counts(session, session_id, archive_path)

    return _final_status(
        session_id=session_id,
        session=session,
        live_exists=live_exists,
        archive_exists=archive_exists,
        raw_record_count=raw_record_count,
        parsed_message_count=parsed_message_count,
        detected_source=detected_source,
        parse_failed=parse_failed,
    )
