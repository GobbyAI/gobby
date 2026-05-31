"""Unified transcript read layer: live transcript file -> gzip archive.

Reads from live transcript files for active/paused sessions and falls back to
gzip archives for expired sessions. Supports JSONL and native JSON transcripts.

Rendered reads are **windowed** through a cached per-session boundary index
(:mod:`gobby.sessions.transcript_index`) so daemon RAM stays bounded on very
large sessions. Legacy flat reads stream the source with an early stop at
``offset + limit`` instead of materializing the whole file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.sessions.transcript_archive import get_archive_dir
from gobby.sessions.transcript_index import (
    SOURCE_SAMPLE_LINES,
    detect_source_bounded,
    get_or_build_index,
)
from gobby.sessions.transcript_io import (
    DecompressionError,
    TranscriptTooLargeError,
    _iter_archive_lines,
    _iter_jsonl_lines,
    _read_archive_lines,
    _read_json_file,
    clear_archive_cache,
)
from gobby.sessions.transcript_limits import (
    LEGACY_LIMIT_MAX,
    NATIVE_JSON_MAX_BYTES,
    RENDERED_LIMIT_MAX,
)
from gobby.sessions.transcript_parsing import (
    _get_parser,
    _parse_json_session,
    _parse_lines,
    _parsed_to_dicts,
)
from gobby.sessions.transcript_paths import _find_transcript_on_disk, _is_json_session_file
from gobby.sessions.transcript_renderer import render_transcript
from gobby.sessions.transcript_source import _resolve_effective_source
from gobby.sessions.transcript_status import get_transcript_status_for_session
from gobby.sessions.transcript_window import (
    MAX_WINDOW_SPAN_BYTES,
    WindowResult,
    _requested_range,
    render_window,
)
from gobby.sessions.transcripts.base import ParsedMessage, RawLine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gobby.sessions.transcript_index import TranscriptIndex
    from gobby.sessions.transcript_renderer import RenderedMessage
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

__all__ = ["TranscriptReader", "clear_archive_cache"]


@dataclass(slots=True)
class _Windowable:
    """A resolved transcript snapshot ready for windowed reads.

    ``kind`` is ``"jsonl"`` (byte-seek), ``"archive"`` (line-seek, ``lines``
    populated), ``"native"`` (no windowing — render whole, ``size`` set for the
    guard), or ``"missing"``.
    """

    kind: str
    path: str | None = None
    source: str | None = None
    index: TranscriptIndex | None = None
    lines: list[str] | None = None
    size: int = 0


def _activity_counts_from_index(index: TranscriptIndex) -> dict[str, int]:
    return {
        "message_count": index.parsed_message_count,
        "turn_count": sum(1 for boundary in index.boundaries if boundary.role == "assistant"),
        "tool_call_count": len(index.tool_first_open),
    }


def _activity_counts_from_messages(messages: list[ParsedMessage]) -> dict[str, int]:
    return {
        "message_count": len(messages),
        "turn_count": sum(1 for msg in messages if msg.role == "assistant"),
        "tool_call_count": sum(
            1
            for msg in messages
            if msg.content_type in ("tool_use", "mcp_tool_use") and msg.tool_use_id
        ),
    }


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

    # ------------------------------------------------------------------ #
    # Legacy flat messages (streaming, limit-capped)
    # ------------------------------------------------------------------ #

    async def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get flat per-message rows, streaming the source with an early stop.

        Reads at most ``offset + limit`` matching rows before stopping, so a
        large transcript never fully materializes. ``format=legacy`` callers
        clamp ``limit`` upstream.
        """
        session = self._session_manager.get(session_id)
        if not session:
            return []

        limit = min(max(0, int(limit)), LEGACY_LIMIT_MAX)
        offset = max(0, int(offset))
        if limit == 0:
            return []
        cap = offset + limit

        path = await self._get_live_transcript_path(session_id, session)
        if path and os.path.isfile(path):
            if _is_json_session_file(path):
                return await self._legacy_native(session_id, offset, limit, role)
            source = await asyncio.to_thread(
                detect_source_bounded, path, session_source=session.source
            )
            dicts = await asyncio.to_thread(
                _collect_legacy_from_file, path, source, session_id, cap, role
            )
            return dicts[offset : offset + limit]

        if session.external_id:
            archive_path = get_archive_dir(self._archive_dir) / f"{session.external_id}.jsonl.gz"
            if archive_path.is_file():
                try:
                    sample = await asyncio.to_thread(
                        _read_archive_sample, str(archive_path), SOURCE_SAMPLE_LINES
                    )
                    source = self._archive_source(session, sample, session_id)
                    dicts = await asyncio.to_thread(
                        _collect_legacy_from_archive,
                        str(archive_path),
                        source,
                        session_id,
                        cap,
                        role,
                    )
                except DecompressionError as e:
                    logger.warning(f"Failed to read archive for session {session_id}: {e}")
                    return []
                return dicts[offset : offset + limit]

        return []

    async def _legacy_native(
        self, session_id: str, offset: int, limit: int, role: str | None
    ) -> list[dict[str, Any]]:
        """Flat-row read for native JSON (small; rendered whole then sliced)."""
        parsed = await self._get_parsed_messages_from_file(session_id)
        dicts = _parsed_to_dicts(parsed)
        filtered = _filter_messages(dicts, session_id=session_id, role=role)
        return filtered[offset : offset + limit]

    # ------------------------------------------------------------------ #
    # Windowed rendered messages
    # ------------------------------------------------------------------ #

    async def get_rendered_window(
        self,
        session_id: str,
        limit: int,
        offset: int,
        order: str = "tail",
        *,
        max_span: int = MAX_WINDOW_SPAN_BYTES,
    ) -> WindowResult:
        """Render a bounded window of rendered groups off the event loop.

        Resolves the live JSONL (byte-seek) / archive (line-seek) snapshot,
        builds-or-reuses its cached boundary index, and delegates to
        :func:`render_window`. Native-JSON transcripts have no line offsets and
        are rendered whole with a size guard (raising
        :class:`TranscriptTooLargeError` above the cap).
        """
        session = self._session_manager.get(session_id)
        if not session:
            return WindowResult(groups=[], returned_count=0, total_groups=0)

        try:
            resolved = await self._resolve_windowable(session, session_id)
        except DecompressionError as e:
            logger.warning(f"Failed to read archive for session {session_id}: {e}")
            return WindowResult(groups=[], returned_count=0, total_groups=0)

        if resolved.kind == "native":
            return await self._native_json_window(
                session_id, resolved.path or "", resolved.size, limit, offset, order
            )
        if resolved.index is None or resolved.path is None:
            return WindowResult(groups=[], returned_count=0, total_groups=0)

        return await asyncio.to_thread(
            render_window,
            resolved.path,
            resolved.source or "claude",
            session_id,
            resolved.index,
            limit=limit,
            offset=offset,
            order=order,
            lines=resolved.lines,
            max_span=max_span,
        )

    async def iter_rendered_windows(
        self,
        session_id: str,
        *,
        page: int = RENDERED_LIMIT_MAX,
        order: str = "head",
    ) -> AsyncIterator[list[RenderedMessage]]:
        """Yield successive rendered-group pages without holding a full render.

        Used by full-transcript scanners (e.g. MCP search). ``order="head"``
        preserves chronological scan order; each page advances by the prior
        page's ``returned_count`` so degraded short pages still compose without
        gaps.
        """
        offset = 0
        while True:
            result = await self.get_rendered_window(session_id, page, offset, order=order)
            if not result.groups:
                break
            yield result.groups
            offset += result.returned_count
            if offset >= result.total_groups:
                break

    async def get_rendered_messages(
        self,
        session_id: str,
        limit: int | None = 100,
        offset: int = 0,
    ) -> list[RenderedMessage]:
        """Get grouped, rendered messages (chronological, bounded).

        Unbounded reads are forbidden — a ``None``/oversized ``limit`` is clamped
        to :data:`RENDERED_LIMIT_MAX` so this can never reintroduce a full
        render. Callers needing every group must page via
        :meth:`iter_rendered_windows`.
        """
        clamped = (
            RENDERED_LIMIT_MAX if limit is None else min(max(int(limit), 0), RENDERED_LIMIT_MAX)
        )
        result = await self.get_rendered_window(
            session_id, clamped, max(0, int(offset)), order="head"
        )
        return result.groups

    async def _native_json_window(
        self,
        session_id: str,
        path: str,
        size: int,
        limit: int,
        offset: int,
        order: str,
    ) -> WindowResult:
        """Render a native-JSON transcript whole, then slice (size-guarded)."""
        if size > NATIVE_JSON_MAX_BYTES:
            raise TranscriptTooLargeError(size, NATIVE_JSON_MAX_BYTES)

        parsed = await self._get_parsed_messages_from_file(session_id)
        if not parsed:
            return WindowResult(groups=[], returned_count=0, total_groups=0)

        rendered = await asyncio.to_thread(render_transcript, parsed, session_id=session_id)
        total = len(rendered)
        g_start, g_end = _requested_range(total, max(0, int(limit)), max(0, int(offset)), order)
        groups = rendered[g_start:g_end]
        return WindowResult(
            groups=groups,
            returned_count=len(groups),
            total_groups=total,
            parsed_message_count=len(parsed),
        )

    # ------------------------------------------------------------------ #
    # Counts / status
    # ------------------------------------------------------------------ #

    async def count_messages(self, session_id: str) -> int:
        """Count parsed messages for a session from the cached index."""
        session = self._session_manager.get(session_id)
        if not session:
            return 0

        try:
            resolved = await self._resolve_windowable(session, session_id)
            if resolved.kind == "native":
                return len(await self._get_parsed_messages_from_file(session_id))
        except (DecompressionError, TranscriptTooLargeError) as e:
            logger.warning(f"Failed to count transcript messages for session {session_id}: {e}")
            return 0

        if resolved.index is not None:
            return resolved.index.parsed_message_count
        return 0

    async def get_activity_counts(self, session_id: str) -> dict[str, int]:
        """Count rendered turns and tool calls from the latest transcript snapshot."""
        session = self._session_manager.get(session_id)
        if not session:
            return {"message_count": 0, "turn_count": 0, "tool_call_count": 0}

        try:
            resolved = await self._resolve_windowable(session, session_id)
        except DecompressionError as e:
            logger.warning(f"Failed to read archive for session {session_id}: {e}")
            return {"message_count": 0, "turn_count": 0, "tool_call_count": 0}

        if resolved.kind == "native":
            return _activity_counts_from_messages(
                await self._get_parsed_messages_from_file(session_id)
            )
        if resolved.index is not None:
            return _activity_counts_from_index(resolved.index)
        return {"message_count": 0, "turn_count": 0, "tool_call_count": 0}

    async def get_transcript_status(self, session_id: str) -> dict[str, Any]:
        """Report transcript availability and parseability for a session."""
        return await get_transcript_status_for_session(
            session_manager=self._session_manager,
            archive_dir=self._archive_dir,
            session_id=session_id,
            get_live_transcript_path=self._get_live_transcript_path,
        )

    # ------------------------------------------------------------------ #
    # Snapshot resolution
    # ------------------------------------------------------------------ #

    async def _resolve_windowable(self, session: Session, session_id: str) -> _Windowable:
        """Resolve the current transcript snapshot and its cached boundary index.

        One disk resolution feeds both counts and windowed renders. Live JSONL
        builds a byte-seek index; the archive fallback decompresses once and
        builds a line-seek index; native JSON is flagged for whole-render.
        """
        path = await self._get_live_transcript_path(session_id, session)
        if path and os.path.isfile(path):
            st = await asyncio.to_thread(os.stat, path)
            if _is_json_session_file(path):
                return _Windowable(kind="native", path=path, source=session.source, size=st.st_size)
            source = await asyncio.to_thread(
                detect_source_bounded, path, session_source=session.source
            )
            index = await get_or_build_index(
                path,
                source,
                session_id,
                seek_mode="byte",
                mtime_ns=st.st_mtime_ns,
                size=st.st_size,
            )
            return _Windowable(kind="jsonl", path=path, source=source, index=index, size=st.st_size)

        if session.external_id:
            archive_path = get_archive_dir(self._archive_dir) / f"{session.external_id}.jsonl.gz"
            if archive_path.is_file():
                st = await asyncio.to_thread(os.stat, str(archive_path))
                lines = await asyncio.to_thread(_read_archive_lines, str(archive_path))
                source = self._archive_source(session, lines[:SOURCE_SAMPLE_LINES], session_id)
                index = await get_or_build_index(
                    str(archive_path),
                    source,
                    session_id,
                    seek_mode="line",
                    lines=lines,
                    mtime_ns=st.st_mtime_ns,
                    size=st.st_size,
                )
                return _Windowable(
                    kind="archive",
                    path=str(archive_path),
                    source=source,
                    index=index,
                    lines=lines,
                    size=st.st_size,
                )

        return _Windowable(kind="missing")

    def _archive_source(self, session: Session, sample: list[str], session_id: str) -> str:
        """Resolve the parser source for an archive from a bounded line sample."""
        effective, _ = _resolve_effective_source(
            session, transcript_path=None, lines=sample, session_id=session_id
        )
        return effective

    # ------------------------------------------------------------------ #
    # Parsed-message helpers (used by native render, counts, status)
    # ------------------------------------------------------------------ #

    async def _get_live_transcript_path(self, session_id: str, session: Session) -> str | None:
        """Resolve a usable live transcript path for a session."""
        transcript_path = getattr(session, "transcript_path", None)
        source = session.source or "claude"
        return await self._ensure_transcript_path(session_id, session, source, transcript_path)

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
        """Read and parse ParsedMessages from live transcript file.

        Used only for native-JSON whole renders/counts; large JSONL transcripts
        go through the windowed index path instead.
        """
        session = self._session_manager.get(session_id)
        if not session:
            return []

        transcript_path = await self._get_live_transcript_path(session_id, session)
        if not transcript_path:
            return []

        try:
            if _is_json_session_file(transcript_path):
                size = await asyncio.to_thread(os.path.getsize, transcript_path)
                if size > NATIVE_JSON_MAX_BYTES:
                    raise TranscriptTooLargeError(size, NATIVE_JSON_MAX_BYTES)
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

            lines = await asyncio.to_thread(_read_all_jsonl_lines, transcript_path)
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
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(f"Failed to read transcript for session {session_id}: {e}")
            return []

    @staticmethod
    def _read_json_file(path: str) -> dict[str, Any]:
        """Read and parse a JSON file. Runs in a thread."""
        return _read_json_file(path)


# --------------------------------------------------------------------------- #
# Module-level streaming helpers (run inside worker threads)
# --------------------------------------------------------------------------- #


def _read_all_jsonl_lines(path: str) -> list[str]:
    """Materialize all JSONL lines (native/whole-render fallback only)."""
    return list(_iter_jsonl_lines(path))


def _read_archive_sample(path: str, max_lines: int) -> list[str]:
    """Read a bounded prefix of decompressed archive lines for source detection."""
    out: list[str] = []
    for line in _iter_archive_lines(path):
        out.append(line)
        if len(out) >= max_lines:
            break
    return out


def _collect_legacy_from_file(
    path: str, source: str, session_id: str, cap: int, role: str | None
) -> list[dict[str, Any]]:
    """Stream a live JSONL file into flat rows, stopping at ``cap`` matches."""
    parser = _get_parser(source, session_id=session_id, transcript_path=path)
    raws = (
        RawLine(byte_offset=None, raw_line_no=i, text=text)
        for i, text in enumerate(_iter_jsonl_lines(path))
    )
    return _collect_legacy_dicts(parser, raws, session_id=session_id, cap=cap, role=role)


def _collect_legacy_from_archive(
    path: str, source: str, session_id: str, cap: int, role: str | None
) -> list[dict[str, Any]]:
    """Stream a gzip archive into flat rows, stopping at ``cap`` matches."""
    parser = _get_parser(source, session_id=session_id, transcript_path=path)
    raws = (
        RawLine(byte_offset=None, raw_line_no=i, text=text)
        for i, text in enumerate(_iter_archive_lines(path))
    )
    return _collect_legacy_dicts(parser, raws, session_id=session_id, cap=cap, role=role)


def _collect_legacy_dicts(
    parser: Any,
    raws: Any,
    *,
    session_id: str,
    cap: int,
    role: str | None,
) -> list[dict[str, Any]]:
    """Drive the streaming parser into flat dict rows with an early stop.

    Stops as soon as ``cap`` (``offset + limit``) matching rows are collected;
    returning early closes the parser's generator, releasing the file handle.
    """
    out: list[dict[str, Any]] = []
    for event in parser.iter_parse_events(raws, start_index=0):
        for record in event.records:
            if not isinstance(record, ParsedMessage):
                continue
            row = _parsed_to_dicts([record])[0]
            row["session_id"] = session_id
            if role and row.get("role") != role:
                continue
            out.append(row)
            if len(out) >= cap:
                return out
    return out


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
