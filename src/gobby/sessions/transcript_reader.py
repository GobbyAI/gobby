"""Unified transcript read layer: live transcript file -> gzip archive.

Reads from live transcript files for active/paused sessions and falls back to
gzip archives for expired sessions. Supported CLI transcripts are line-oriented,
including Qwen's ``.json`` envelope files.

Rendered reads are **windowed** through a cached per-session boundary index
(:mod:`gobby.sessions.transcript_index`) so daemon RAM stays bounded on very
large sessions. Flat reads stream the source with an early stop at ``offset +
limit`` instead of materializing the whole file.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.sessions.gzip_seek_index import (
    GZIP_BLOCK_SEEK_MODE,
    GzipBlockIndex,
    ensure_gzip_block_index,
    iter_gzip_block_raw_lines,
)
from gobby.sessions.machine_scope import require_local_session_ownership
from gobby.sessions.observation_tracker import ObservationTracker
from gobby.sessions.transcript_archive import get_archive_dir
from gobby.sessions.transcript_index import (
    SOURCE_SAMPLE_LINES,
    detect_source_bounded,
    get_or_build_index,
)
from gobby.sessions.transcript_io import (
    DecompressionError,
    _iter_archive_lines,
    _iter_jsonl_lines,
    clear_archive_cache,
)
from gobby.sessions.transcript_limits import FLAT_ROW_LIMIT_MAX, RENDERED_LIMIT_MAX
from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcript_parsing import _parsed_to_dicts
from gobby.sessions.transcript_paths import MISSING_TRANSCRIPT_PATH, find_transcript_on_disk
from gobby.sessions.transcript_source import _resolve_effective_source
from gobby.sessions.transcript_status import get_transcript_status_for_session
from gobby.sessions.transcript_window import (
    MAX_WINDOW_SPAN_BYTES,
    WindowResult,
    render_window,
)
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import (
    ParsedMessage,
    RawLine,
)
from gobby.storage.unmodeled_observations import UnmodeledObservationStore

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

    ``kind`` is ``"jsonl"`` (byte-seek), ``"archive"`` (gzip-block seek), or
    ``"missing"``.
    """

    kind: str
    path: str | None = None
    source: str | None = None
    index: TranscriptIndex | None = None
    lines: list[str] | None = None
    gzip_index: GzipBlockIndex | None = None
    size: int = 0


def _activity_counts_from_index(index: TranscriptIndex) -> dict[str, int]:
    return {
        "message_count": index.parsed_message_count,
        "turn_count": sum(1 for boundary in index.boundaries if boundary.role == "assistant"),
        "tool_call_count": len(index.tool_first_open),
    }


class TranscriptReader:
    """Unified read layer: live transcript first, gzip archive fallback."""

    def __init__(
        self,
        session_manager: SessionManager,
        archive_dir: str | None = None,
        observation_store: UnmodeledObservationStore | None = None,
    ):
        self._session_manager = session_manager
        self._archive_dir = archive_dir
        self._observation_store = observation_store

    # ------------------------------------------------------------------ #
    # Flat messages (streaming, limit-capped)
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
        large transcript never fully materializes.
        """
        session = self._session_manager.get(session_id)
        if not session:
            return []

        require_local_session_ownership(session)

        limit = min(max(0, int(limit)), FLAT_ROW_LIMIT_MAX)
        offset = max(0, int(offset))
        if limit == 0:
            return []
        cap = offset + limit

        path = await self._get_live_transcript_path(session_id, session)
        if path and os.path.isfile(path):
            source = await asyncio.to_thread(
                detect_source_bounded, path, session_source=session.source
            )
            st = await asyncio.to_thread(os.stat, path)
            index = await get_or_build_index(
                path,
                source,
                session_id,
                seek_mode="byte",
                mtime_ns=st.st_mtime_ns,
                size=st.st_size,
            )
            dicts = await asyncio.to_thread(
                _collect_flat_from_file_windowed,
                path,
                source,
                session_id,
                index,
                offset,
                limit,
                role,
            )
            return dicts

        if session.external_id:
            archive_path = get_archive_dir(self._archive_dir) / f"{session.external_id}.jsonl.gz"
            if archive_path.is_file():
                try:
                    sample = await asyncio.to_thread(
                        _read_archive_sample, str(archive_path), SOURCE_SAMPLE_LINES
                    )
                    source = self._archive_source(session, sample, session_id)
                    dicts = await asyncio.to_thread(
                        _collect_flat_from_archive,
                        str(archive_path),
                        source,
                        session_id,
                        cap,
                        role,
                    )
                except DecompressionError as e:
                    logger.warning("Failed to read archive for session %s: %s", session_id, e)
                    return []
                return dicts[offset : offset + limit]

        return []

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

        Resolves the live line-oriented (byte-seek) / archive (line-seek)
        snapshot, builds-or-reuses its cached boundary index, and delegates to
        :func:`render_window`.
        """
        session = self._session_manager.get(session_id)
        if not session:
            return WindowResult(groups=[], returned_count=0, total_groups=0)

        try:
            resolved = await self._resolve_windowable(session, session_id)
        except DecompressionError as e:
            logger.warning("Failed to read archive for session %s: %s", session_id, e)
            return WindowResult(groups=[], returned_count=0, total_groups=0)

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
            gzip_index=resolved.gzip_index,
            max_span=max_span,
            observation_tracker=ObservationTracker(self._observation_store),
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
        except DecompressionError as e:
            logger.warning("Failed to count transcript messages for session %s: %s", session_id, e)
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
            if resolved.index is not None:
                return _activity_counts_from_index(resolved.index)
            return {"message_count": 0, "turn_count": 0, "tool_call_count": 0}
        except DecompressionError as e:
            logger.warning("Failed to count transcript activity for session %s: %s", session_id, e)
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

        One disk resolution feeds both counts and windowed renders. Live
        line-oriented transcripts build a byte-seek index; the archive fallback
        decompresses once and builds a line-seek index.
        """
        require_local_session_ownership(session)
        path = await self._get_live_transcript_path(session_id, session)
        if path and os.path.isfile(path):
            st = await asyncio.to_thread(os.stat, path)
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
                sample = await asyncio.to_thread(
                    _read_archive_sample, str(archive_path), SOURCE_SAMPLE_LINES
                )
                source = self._archive_source(session, sample, session_id)
                gzip_index = await ensure_gzip_block_index(
                    str(archive_path),
                    mtime_ns=st.st_mtime_ns,
                    size=st.st_size,
                )
                st = await asyncio.to_thread(os.stat, str(archive_path))
                index = await get_or_build_index(
                    str(archive_path),
                    source,
                    session_id,
                    seek_mode=GZIP_BLOCK_SEEK_MODE,
                    raw_lines=iter_gzip_block_raw_lines(str(archive_path), gzip_index, 0, 0),
                    logical_size=gzip_index.uncompressed_size,
                    mtime_ns=st.st_mtime_ns,
                    size=st.st_size,
                )
                return _Windowable(
                    kind="archive",
                    path=str(archive_path),
                    source=source,
                    index=index,
                    gzip_index=gzip_index,
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
        require_local_session_ownership(session)
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
        local_machine_id = require_local_session_ownership(session)
        if (
            transcript_path
            and transcript_path != MISSING_TRANSCRIPT_PATH
            and os.path.isfile(transcript_path)
        ):
            return transcript_path

        external_id = getattr(session, "external_id", None)
        derived = await asyncio.to_thread(
            find_transcript_on_disk,
            source,
            external_id or "",
            owner_machine_id=session.machine_id,
            local_machine_id=local_machine_id,
        )
        if not derived:
            return None

        try:
            await asyncio.to_thread(
                self._session_manager.update, session_id, transcript_path=derived
            )
            logger.info("Re-derived transcript path for session %s: %s", session_id, derived)
        except (OSError, ValueError) as e:
            logger.warning(
                "Failed to persist re-derived transcript path for session %s (%s): %s",
                session_id,
                derived,
                e,
            )
        return derived


# --------------------------------------------------------------------------- #
# Module-level streaming helpers (run inside worker threads)
# --------------------------------------------------------------------------- #


def _read_archive_sample(path: str, max_lines: int) -> list[str]:
    """Read a bounded prefix of decompressed archive lines for source detection."""
    out: list[str] = []
    for line in _iter_archive_lines(path):
        out.append(line)
        if len(out) >= max_lines:
            break
    return out


def _collect_flat_from_file(
    path: str, source: str, session_id: str, cap: int, role: str | None
) -> list[dict[str, Any]]:
    """Stream a live JSONL file into flat rows, stopping at ``cap`` matches."""
    parser = get_parser(source, session_id=session_id, transcript_path=path)
    raws = (
        RawLine(byte_offset=None, raw_line_no=i, text=text)
        for i, text in enumerate(_iter_jsonl_lines(path))
    )
    return _collect_flat_dicts(
        parser, raws, source=source, session_id=session_id, cap=cap, role=role
    )


def _iter_jsonl_raw_lines_from(
    path: str, start_byte: int, start_line_no: int, size: int
) -> Iterator[RawLine]:
    offset = start_byte
    line_no = start_line_no
    with open(path, "rb") as handle:
        handle.seek(start_byte)
        for raw_bytes in handle:
            if offset >= size:
                break
            yield RawLine(
                byte_offset=offset,
                raw_line_no=line_no,
                text=raw_bytes.decode("utf-8", errors="replace"),
            )
            offset += len(raw_bytes)
            line_no += 1


def _collect_flat_from_file_windowed(
    path: str,
    source: str,
    session_id: str,
    index: TranscriptIndex,
    offset: int,
    limit: int,
    role: str | None,
) -> list[dict[str, Any]]:
    """Stream flat rows from the closest parsed checkpoint for live JSONL."""
    boundary = index.parsed_boundary_for_offset(offset, role)
    if boundary is None or boundary.byte_start is None:
        cap = offset + limit
        return _collect_flat_from_file(path, source, session_id, cap, role)[offset : offset + limit]

    parser = get_parser(source, session_id=session_id, transcript_path=path)
    parser.hydrate_state(index.parser_state)
    raws = _iter_jsonl_raw_lines_from(
        path, boundary.byte_start, boundary.raw_line_start, index.size
    )
    seen = boundary.role_counts_start.get(role, 0) if role else boundary.message_index_start
    skip = max(0, offset - seen)
    out: list[dict[str, Any]] = []
    for event in parser.iter_parse_events(raws, start_index=boundary.parsed_index_start):
        for record in normalize_transcript_records(event.records, source):
            if not isinstance(record, ParsedMessage):
                continue
            rows = _parsed_to_dicts([record])
            if not rows:
                # Session metadata (titles, unmodeled-record sentinel) flattens
                # to nothing — skip without IndexError.
                continue
            row = rows[0]
            row["session_id"] = session_id
            if role and row.get("role") != role:
                continue
            if skip > 0:
                skip -= 1
                continue
            out.append(row)
            if len(out) >= limit:
                return out
    return out


def _collect_flat_from_archive(
    path: str, source: str, session_id: str, cap: int, role: str | None
) -> list[dict[str, Any]]:
    """Stream a gzip archive into flat rows, stopping at ``cap`` matches."""
    parser = get_parser(source, session_id=session_id, transcript_path=path)
    raws = (
        RawLine(byte_offset=None, raw_line_no=i, text=text)
        for i, text in enumerate(_iter_archive_lines(path))
    )
    return _collect_flat_dicts(
        parser, raws, source=source, session_id=session_id, cap=cap, role=role
    )


def _collect_flat_dicts(
    parser: Any,
    raws: Any,
    *,
    source: str,
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
        for record in normalize_transcript_records(event.records, source):
            if not isinstance(record, ParsedMessage):
                continue
            rows = _parsed_to_dicts([record])
            if not rows:
                # Session metadata (titles, unmodeled-record sentinel) flattens
                # to nothing — skip without IndexError.
                continue
            row = rows[0]
            row["session_id"] = session_id
            if role and row.get("role") != role:
                continue
            out.append(row)
            if len(out) >= cap:
                return out
    return out
