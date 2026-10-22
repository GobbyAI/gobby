"""Locate owned provider transcripts and select a bounded parsing window."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gobby.sessions.machine_scope import require_local_session_ownership
from gobby.sessions.transcript_archive import get_archive_dir
from gobby.sessions.transcript_paths import (
    find_supplemental_transcripts_on_disk,
    find_transcript_on_disk,
)
from gobby.sessions.transcripts.base import RawLine, raw_lines_from_texts
from gobby.storage.session_models import Session

WINDOW_LOOKBACK = timedelta(hours=2)


_UTC_LINE_TIMESTAMP_RE = re.compile(
    r'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2}T[0-9:.]{8,})(?:Z|\+00:00)"'
)


def select_window_raw_lines(
    lines: Iterable[str],
    window_start: datetime | None,
) -> Iterator[RawLine]:
    """Yield raw lines the claim window can still reach, keeping line numbers.

    A line is dropped only when it carries an unambiguously UTC timestamp
    older than :data:`WINDOW_LOOKBACK` before the window. Provider cwd metadata,
    later lines, undated lines, and timestamps in another offset are kept, so
    filtering preserves the context that later tool calls depend on.
    """
    if window_start is None:
        yield from raw_lines_from_texts(lines)
        return
    cutoff = (_as_utc(window_start) - WINDOW_LOOKBACK).strftime("%Y-%m-%dT%H:%M:%S")
    for index, text in enumerate(lines):
        # Every timestamp on the line must be older, not just the first one:
        # the top-level timestamp sits near the end of a Claude JSONL line, so
        # an unescaped nested one earlier in the line would otherwise shadow it
        # and strand a live validation run.
        stamps = [match.group(1) for match in _UTC_LINE_TIMESTAMP_RE.finditer(text)]
        if stamps and all(stamp < cutoff for stamp in stamps):
            # Provider metadata establishes the cwd for later relative tools,
            # even when a long turn began before this task's lookback window.
            if '"session_meta"' not in text and '"turn_context"' not in text:
                continue
            try:
                context = json.loads(text)
            except ValueError:
                continue
            if not isinstance(context, dict) or context.get("type") not in {
                "session_meta",
                "turn_context",
            }:
                continue
        yield RawLine(byte_offset=None, raw_line_no=index, text=text)


def _resolve_transcript_path(
    session: Session,
    archive_dir: str | None,
    local_machine_id: str | None = None,
) -> tuple[str | None, list[str]]:
    local_machine_id = local_machine_id or require_local_session_ownership(session)
    attempted: list[str] = []
    if session.transcript_path:
        attempted.append(session.transcript_path)
        if Path(session.transcript_path).is_file():
            return session.transcript_path, attempted

    discovered = find_transcript_on_disk(
        session.source,
        session.external_id,
        owner_machine_id=session.machine_id,
        local_machine_id=local_machine_id,
        caller_context="recovery",
    )
    if discovered:
        attempted.append(discovered)
        if Path(discovered).is_file():
            return discovered, attempted

    archive_path = get_archive_dir(archive_dir) / f"{session.external_id}.jsonl.gz"
    attempted.append(str(archive_path))
    if archive_path.is_file():
        return str(archive_path), attempted
    return None, attempted


def _resolve_transcript_paths(
    session: Session,
    archive_dir: str | None,
    local_machine_id: str,
    window_start: datetime | None = None,
) -> tuple[list[str], list[str]]:
    primary, attempted = _resolve_transcript_path(session, archive_dir, local_machine_id)
    if primary is None:
        return [], attempted
    supplemental = find_supplemental_transcripts_on_disk(session.source, primary)
    # A compact or account switch can replace a live file at the same path.
    # Read this session's archive as well; never scan other sessions' files.
    archive = str(get_archive_dir(archive_dir) / f"{session.external_id}.jsonl.gz")
    if Path(archive).is_file():
        if window_start is not None and Path(archive).stat().st_mtime < window_start.timestamp():
            if archive == primary:
                return [], attempted
            supplemental = [path for path in supplemental if path != archive]
        elif archive != primary:
            supplemental.append(archive)
    paths = list(dict.fromkeys((primary, *supplemental)))
    return paths, list(dict.fromkeys((*attempted, *supplemental)))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
