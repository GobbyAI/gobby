"""Unified transcript read layer: live transcript file → gzip archive.

Reads from the live transcript file on disk (active/paused sessions).
Supports both JSONL (Claude, Codex) and native JSON (Gemini) formats.
If no transcript exists (cleaned up after expiry), falls back to the gzip archive.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import json
import logging
import os
import zlib
from typing import TYPE_CHECKING, Any

from gobby.sessions.transcript_archive import get_archive_dir

if TYPE_CHECKING:
    from gobby.sessions.transcript_renderer import RenderedMessage
    from gobby.sessions.transcripts.base import ParsedMessage
    from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
    from gobby.sessions.transcripts.codex import CodexTranscriptParser
    from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
    from gobby.sessions.transcripts.qwen import QwenTranscriptParser
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import LocalSessionManager

    TranscriptParser = (
        ClaudeTranscriptParser | GeminiTranscriptParser | QwenTranscriptParser | CodexTranscriptParser
    )

from pathlib import Path

from gobby.sessions.transcript_renderer import render_transcript

logger = logging.getLogger(__name__)


def _count_nonempty_lines(lines: list[str]) -> int:
    """Count non-empty JSONL records."""
    return sum(1 for line in lines if line.strip())


def _detect_source_from_path(path: str | None) -> str | None:
    """Infer transcript source from a known path shape."""
    if not path:
        return None

    normalized = str(Path(path).expanduser())
    lowered = normalized.lower()
    parts = Path(normalized).parts

    if ".codex" in parts and "sessions" in parts:
        return "codex"
    if Path(normalized).name.startswith("rollout-") and lowered.endswith(".jsonl"):
        return "codex"
    if ".qwen" in parts:
        return "qwen"
    if ".gemini" in parts or lowered.endswith(".json"):
        return "gemini"
    if ".claude" in parts and "projects" in parts:
        return "claude"

    return None


def _load_json_object(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object, returning None for non-dict or invalid values."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _detect_source_from_record(data: dict[str, Any]) -> str | None:
    """Infer transcript source from a decoded transcript record."""
    if "sessionId" in data or isinstance(data.get("messages"), list):
        return "gemini"

    line_type = data.get("type")
    payload = data.get("payload")
    message = data.get("message")

    if isinstance(payload, dict) and line_type in {
        "response_item",
        "event_msg",
        "session_meta",
        "turn_context",
    }:
        return "codex"

    if isinstance(message, dict):
        if "role" in message:
            return "claude"
        if "content" in message:
            return "claude"

    if line_type in {"assistant", "summary", "system"}:
        return "claude"
    if line_type == "user":
        return "claude" if isinstance(message, dict) else "gemini"

    if line_type in {"init", "message", "tool_use", "tool_result", "result", "model", "gemini"}:
        return "gemini"

    return None


def _detect_source_from_json_session(data: dict[str, Any]) -> str | None:
    """Infer transcript source from a native JSON session file."""
    if "sessionId" in data or isinstance(data.get("messages"), list):
        return "gemini"
    return _detect_source_from_record(data)


def _detect_source_from_jsonl_lines(lines: list[str]) -> str | None:
    """Infer transcript source from JSONL content."""
    for raw_line in lines:
        if not raw_line.strip():
            continue
        data = _load_json_object(raw_line)
        if not data:
            continue
        detected = _detect_source_from_record(data)
        if detected:
            return detected
    return None


def _resolve_effective_source(
    session: Session,
    *,
    transcript_path: str | None = None,
    lines: list[str] | None = None,
    data: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> tuple[str, str | None]:
    """Choose the parser source from transcript evidence, with DB source as fallback."""
    detected_source = _detect_source_from_path(transcript_path)
    if detected_source is None and data is not None:
        detected_source = _detect_source_from_json_session(data)
    if detected_source is None and lines is not None:
        detected_source = _detect_source_from_jsonl_lines(lines)

    effective_source = detected_source or session.source or "claude"
    stored_source = getattr(session, "source", None)
    if detected_source and stored_source and detected_source != stored_source:
        logger.warning(
            "Transcript source mismatch for session %s: stored=%s detected=%s path=%s",
            session_id,
            stored_source,
            detected_source,
            transcript_path,
        )

    return effective_source, detected_source


def _find_transcript_on_disk(
    source: str,
    external_id: str,
    max_days: int = 90,
) -> str | None:
    """Try to find a transcript file on disk by CLI source and external_id.

    Called when transcript_path is missing or invalid. Each CLI stores
    transcripts in a predictable location keyed by session/external ID.

    Returns:
        Absolute path to transcript file, or None if not found.
    """
    if not external_id:
        return None

    if source == "claude":
        # Claude: ~/.claude/projects/{project-path-slug}/{external_id}.jsonl
        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.exists():
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                candidate = proj_dir / f"{external_id}.jsonl"
                if candidate.is_file():
                    return str(candidate)

    elif source == "codex":
        # Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-{datetime}-{external_id}.jsonl
        sessions_dir = Path.home() / ".codex" / "sessions"
        if sessions_dir.exists():
            # Search recent date dirs (walk backwards to find quickly)
            inspected_days = 0
            for year_dir in sorted(sessions_dir.iterdir(), reverse=True):
                if not year_dir.is_dir():
                    continue
                for month_dir in sorted(year_dir.iterdir(), reverse=True):
                    if not month_dir.is_dir():
                        continue
                    for day_dir in sorted(month_dir.iterdir(), reverse=True):
                        if not day_dir.is_dir():
                            continue
                        if inspected_days >= max_days:
                            return None
                        inspected_days += 1
                        matches = list(day_dir.glob(f"*{external_id}*"))
                        if matches:
                            return str(matches[0])

    elif source == "gemini":
        # Gemini: ~/.gemini/tmp/{hash}/chats/session-{date}-{id[:8]}.json
        gemini_tmp = Path.home() / ".gemini" / "tmp"
        prefix = external_id[:8] if external_id else ""
        if gemini_tmp.exists() and prefix:
            for proj_dir in gemini_tmp.iterdir():
                chats_dir = proj_dir / "chats"
                if not chats_dir.is_dir():
                    continue
                matches = sorted(chats_dir.glob(f"session-*-{prefix}.json"), reverse=True)
                if matches:
                    return str(matches[0])
    elif source == "qwen":
        qwen_tmp = Path.home() / ".qwen" / "tmp"
        prefix = external_id[:8] if external_id else ""
        if qwen_tmp.exists() and prefix:
            for proj_dir in qwen_tmp.iterdir():
                chats_dir = proj_dir / "chats"
                if not chats_dir.is_dir():
                    continue
                matches = sorted(chats_dir.glob(f"session-*-{prefix}.json"), reverse=True)
                if matches:
                    return str(matches[0])

    return None


# LRU-cached decompression to avoid repeated gzip reads within a session
_ARCHIVE_CACHE_SIZE = 32


@functools.lru_cache(maxsize=_ARCHIVE_CACHE_SIZE)
def _decompress_archive(archive_path: str) -> list[str]:
    """Decompress a gzip archive and return lines.

    Cached so repeated reads of the same archive don't re-decompress.
    Handles truncated archives gracefully by returning what was read.
    """
    lines = []
    try:
        with gzip.open(archive_path, "rt", encoding="utf-8") as f:
            for line in f:
                lines.append(line)
    except (EOFError, gzip.BadGzipFile, zlib.error) as e:
        logger.warning(f"Truncated or malformed gzip archive {archive_path}: {e}")
    return lines


def _get_parser(source: str, session_id: str | None = None) -> TranscriptParser:
    """Get the appropriate transcript parser for a source."""
    from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
    from gobby.sessions.transcripts.codex import CodexTranscriptParser
    from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
    from gobby.sessions.transcripts.qwen import QwenTranscriptParser

    if source == "gemini":
        return GeminiTranscriptParser(session_id=session_id)
    elif source == "qwen":
        return QwenTranscriptParser(session_id=session_id)
    elif source == "codex":
        return CodexTranscriptParser(session_id=session_id)
    else:
        return ClaudeTranscriptParser(session_id=session_id)


def _parse_lines(
    lines: list[str], source: str, session_id: str | None = None
) -> list[ParsedMessage]:
    """Parse lines into ParsedMessage objects."""
    parser = _get_parser(source, session_id=session_id)
    return parser.parse_lines(lines, start_index=0)


def _parse_json_session(
    data: dict[str, Any], source: str, session_id: str | None = None
) -> list[ParsedMessage]:
    """Parse a native JSON session file (e.g., Gemini/Qwen format)."""
    from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
    from gobby.sessions.transcripts.qwen import QwenTranscriptParser

    if source == "gemini":
        parser = GeminiTranscriptParser(session_id=session_id)
        return parser.parse_session_json(data)
    if source == "qwen":
        parser = QwenTranscriptParser(session_id=session_id)
        return parser.parse_session_json(data)
    # Fallback: wrap as single-line JSONL
    return _parse_lines([json.dumps(data)], source, session_id=session_id)


def _parse_lines_to_dicts(
    lines: list[str],
    source: str,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Parse JSONL lines through the appropriate transcript parser.

    Returns dicts matching the session_messages column shape so callers
    get a consistent format regardless of source.
    """
    parsed = _parse_lines(lines, source, session_id=session_id)
    return _parsed_to_dicts(parsed)


def _parsed_to_dicts(parsed: list[ParsedMessage]) -> list[dict[str, Any]]:
    """Convert ParsedMessage list to dicts."""
    results: list[dict[str, Any]] = []
    for msg in parsed:
        results.append(
            {
                "session_id": None,  # not available from archive
                "message_index": msg.index,
                "role": msg.role,
                "content": msg.content,
                "content_type": msg.content_type,
                "tool_name": msg.tool_name,
                "tool_input": msg.tool_input,
                "tool_result": msg.tool_result,
                "tool_use_id": msg.tool_use_id,
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                "raw_json": msg.raw_json,  # kept for archive writes, not stored in DB
            }
        )
    return results


def _is_json_session_file(path: str) -> bool:
    """Check if a transcript file is a native JSON session file (not JSONL)."""
    return path.endswith(".json")


class TranscriptReader:
    """Unified read layer: live transcript first, gzip archive fallback.

    Supports JSONL (Claude, Codex) and native JSON (Gemini) transcript formats.

    Usage::

        reader = TranscriptReader(session_manager=session_manager)
        messages = await reader.get_messages(session_id, limit=50)
    """

    def __init__(
        self,
        session_manager: LocalSessionManager,
        archive_dir: str | None = None,
        # Deprecated: kept for backwards-compat callers, ignored
        message_manager: object | None = None,
    ):
        if message_manager is not None:
            import warnings

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
        """Get messages for a session, falling back to gzip archive.

        Args:
            session_id: Session UUID
            limit: Maximum messages to return
            offset: Pagination offset
            role: Optional role filter

        Returns:
            List of message dicts
        """
        # 1. Try live transcript file (active/paused sessions)
        has_live_transcript = await self._has_live_transcript(session_id)
        file_messages = await self._read_from_file(session_id, limit, offset, role)
        if has_live_transcript:
            return file_messages

        # 2. Transcript gone — try gzip archive (expired sessions)
        return await self._read_from_archive(session_id, limit, offset, role)

    async def get_rendered_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RenderedMessage]:
        """Get grouped, rendered messages for a session.

        Skips the database entirely (avoids corrupted str() data) and reads
        directly from transcript file or gzip archive.

        Args:
            session_id: Session UUID
            limit: Maximum turns to return
            offset: Pagination offset

        Returns:
            List of RenderedMessage objects
        """
        # 1. Try live transcript file
        has_live_transcript = await self._has_live_transcript(session_id)
        parsed_messages = await self._get_parsed_messages_from_file(session_id)

        # 2. Fallback to gzip archive
        if not has_live_transcript:
            parsed_messages = await self._get_parsed_messages_from_archive(session_id)

        if not parsed_messages:
            return []

        # 3. Render transcript (group blocks into turns)
        rendered = render_transcript(parsed_messages, session_id=session_id)

        # 4. Apply pagination
        return rendered[offset : offset + limit]

    async def count_messages(self, session_id: str) -> int:
        """Count messages for a session from live transcript or gzip archive."""
        session = self._session_manager.get(session_id)
        if not session:
            return 0

        if await self._has_live_transcript(session_id):
            return len(await self._get_parsed_messages_from_file(session_id))

        # Fallback: count lines from gzip archive
        return len(await self._get_parsed_messages_from_archive(session_id))

    async def get_transcript_status(self, session_id: str) -> dict[str, Any]:
        """Report transcript availability and parseability for a session."""
        session = self._session_manager.get(session_id)
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

        transcript_path = await self._get_live_transcript_path(session_id, session)
        live_exists = bool(transcript_path and os.path.isfile(transcript_path))

        archive_exists = False
        archive_path: Path | None = None
        if session.external_id:
            archive_path = get_archive_dir(self._archive_dir) / f"{session.external_id}.jsonl.gz"
            archive_exists = archive_path.is_file()

        raw_record_count = 0
        parsed_message_count = 0
        detected_source: str | None = None
        parse_failed = False

        if live_exists and transcript_path:
            if _is_json_session_file(transcript_path):
                try:
                    data = await asyncio.to_thread(self._read_json_file, transcript_path)
                    raw_record_count = (
                        len(data.get("messages", [])) if isinstance(data, dict) else 0
                    )
                    effective_source, detected_source = _resolve_effective_source(
                        session,
                        transcript_path=transcript_path,
                        data=data,
                        session_id=session_id,
                    )
                    parsed_message_count = len(
                        _parse_json_session(data, effective_source, session_id=session_id)
                    )
                except (json.JSONDecodeError, ValueError, OSError) as e:
                    logger.warning(f"Failed to parse JSON transcript for session {session_id}: {e}")
                    parse_failed = True
            else:
                try:
                    lines = await asyncio.to_thread(self._read_jsonl_lines, transcript_path)
                    raw_record_count = _count_nonempty_lines(lines)
                    effective_source, detected_source = _resolve_effective_source(
                        session,
                        transcript_path=transcript_path,
                        lines=lines,
                        session_id=session_id,
                    )
                    parsed_message_count = len(
                        _parse_lines(lines, effective_source, session_id=session_id)
                    )
                except (json.JSONDecodeError, ValueError, OSError) as e:
                    logger.warning(
                        f"Failed to parse JSONL transcript for session {session_id}: {e}"
                    )
                    parse_failed = True
        elif archive_exists and archive_path is not None:
            try:
                lines = await asyncio.to_thread(_decompress_archive, str(archive_path))
                raw_record_count = _count_nonempty_lines(lines)
                # Pass transcript_path=None so a stale live path can't override
                # content sniffing of the archive we actually decompressed.
                _, detected_source = _resolve_effective_source(
                    session,
                    transcript_path=None,
                    lines=lines,
                    session_id=session_id,
                )
                parsed_message_count = len(await self._get_parsed_messages_from_archive(session_id))
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

    async def _get_parsed_messages_from_archive(self, session_id: str) -> list[ParsedMessage]:
        """Read and parse ParsedMessages from gzip archive."""
        session = self._session_manager.get(session_id)
        if not session or not session.external_id:
            return []

        archive_dir = get_archive_dir(self._archive_dir)
        archive_path = archive_dir / f"{session.external_id}.jsonl.gz"

        if not archive_path.is_file():
            return []

        try:
            lines = await asyncio.to_thread(_decompress_archive, str(archive_path))
            # Prefer content-sniffing over any stale live transcript path so
            # an incorrect session.transcript_path can't mislabel the source.
            source, _ = _resolve_effective_source(
                session,
                transcript_path=None,
                lines=lines,
                session_id=session_id,
            )
            return _parse_lines(lines, source, session_id=session_id)
        except Exception as e:
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
        derived = _find_transcript_on_disk(
            source,
            external_id or "",
        )
        if not derived:
            return None

        try:
            await asyncio.to_thread(
                self._session_manager.update, session_id, transcript_path=derived
            )
            logger.info(f"Re-derived transcript path for session {session_id}: {derived}")
        except (OSError, ValueError) as e:
            # Persistence is best-effort — failure to update transcript_path
            # shouldn't break the read path. Anything other than expected
            # storage/validation errors should still surface.
            logger.warning(
                f"Failed to persist re-derived transcript path for session {session_id} "
                f"({derived}): {e}"
            )
        return derived

    async def _get_parsed_messages_from_file(self, session_id: str) -> list[ParsedMessage]:
        """Read and parse ParsedMessages from live transcript file.

        Handles both JSONL (Claude, Codex) and native JSON (Gemini) formats.
        If transcript_path is missing, tries to re-derive it.
        """
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
                return _parse_json_session(data, source, session_id=session_id)
            else:
                lines = await asyncio.to_thread(self._read_jsonl_lines, transcript_path)
                source, _ = _resolve_effective_source(
                    session,
                    transcript_path=transcript_path,
                    lines=lines,
                    session_id=session_id,
                )
                return _parse_lines(lines, source, session_id=session_id)
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

        archive_dir = get_archive_dir(self._archive_dir)
        archive_path = archive_dir / f"{session.external_id}.jsonl.gz"

        if not archive_path.is_file():
            return []

        try:
            lines = await asyncio.to_thread(_decompress_archive, str(archive_path))
            # Content-sniff first; ignore any stale session.transcript_path here.
            source, _ = _resolve_effective_source(
                session,
                transcript_path=None,
                lines=lines,
                session_id=session_id,
            )
            all_messages = _parse_lines_to_dicts(lines, source, session_id=session_id)
        except Exception as e:
            logger.warning(f"Failed to read archive for session {session_id}: {e}")
            return []

        # Fill in session_id
        for msg in all_messages:
            msg["session_id"] = session_id

        # Apply role filter
        if role:
            all_messages = [m for m in all_messages if m["role"] == role]

        # Apply pagination
        return all_messages[offset : offset + limit]

    async def _read_from_file(
        self,
        session_id: str,
        limit: int,
        offset: int,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read messages from a live transcript file on disk.

        Handles both JSONL and native JSON formats.
        If transcript_path is missing or invalid, tries to re-derive it
        from the CLI's known transcript directory.
        """
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
                parsed = _parse_json_session(data, source, session_id=session_id)
                all_messages = _parsed_to_dicts(parsed)
            else:
                lines = await asyncio.to_thread(self._read_jsonl_lines, transcript_path)
                source, _ = _resolve_effective_source(
                    session,
                    transcript_path=transcript_path,
                    lines=lines,
                    session_id=session_id,
                )
                all_messages = _parse_lines_to_dicts(lines, source, session_id=session_id)
        except Exception as e:
            logger.warning(f"Failed to read transcript for session {session_id}: {e}")
            return []

        # Fill in session_id
        for msg in all_messages:
            msg["session_id"] = session_id

        # Apply role filter
        if role:
            all_messages = [m for m in all_messages if m["role"] == role]

        # Apply pagination
        return all_messages[offset : offset + limit]

    @staticmethod
    def _read_jsonl_lines(path: str) -> list[str]:
        """Read lines from a JSONL file. Runs in a thread."""
        with open(path, encoding="utf-8") as f:
            return f.readlines()

    @staticmethod
    def _read_json_file(path: str) -> dict[str, Any]:
        """Read and parse a JSON file. Runs in a thread."""
        with open(path, encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result


def clear_archive_cache() -> None:
    """Clear the LRU cache for decompressed archives.

    Useful after writing new archives to ensure fresh reads.
    """
    _decompress_archive.cache_clear()
