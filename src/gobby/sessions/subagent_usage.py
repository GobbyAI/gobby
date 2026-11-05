"""Claude subagent calls as token events of the parent session (plan 2.2).

A Claude parent transcript keeps its subagents' calls in sibling files at
``<transcript>/subagents/agent-*.jsonl``. The live processor, the rebuild, and the
audit all read them through :func:`read_subagent_events`, so each path assigns the
same message ids and the same totals.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gobby.sessions.processor_transcripts import _parse_incremental_records
from gobby.sessions.transcript_paths import find_supplemental_transcripts_on_disk
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage
from gobby.sessions.usage_calls import api_call_count
from gobby.storage.token_events import TokenEvent, canonicalize_event_timestamp

logger = logging.getLogger(__name__)

SUBAGENT_SOURCE = "claude"
_AGENT_PREFIX = "agent-"


@dataclass(frozen=True)
class SubagentCursor:
    """Read position in one subagent file.

    ``offset`` is the end of the last complete line read, and ``next_index`` is the
    message index the next line starts at. ``dev``, ``ino`` and ``mtime_ns`` identify
    the file the offset belongs to.
    """

    offset: int = 0
    next_index: int = 0
    dev: int | None = None
    ino: int | None = None
    mtime_ns: int | None = None
    parser_state: Mapping[str, Any] = field(default_factory=dict)


def read_subagent_events(
    session_id: str,
    project_id: str | None,
    transcript_path: str,
    cursors: Mapping[str, SubagentCursor],
) -> tuple[list[TokenEvent], dict[str, SubagentCursor]]:
    """Read new complete lines from each subagent file and return their usage events.

    A file with no cursor, or whose cursor no longer matches it (truncated below the
    offset, replaced by a new inode, or with an older mtime), is read from offset 0 and
    index 0. The returned cursors cover the files present now. The caller stores them
    only after the events persist. A file that cannot be read keeps its prior cursor.
    """
    events: list[TokenEvent] = []
    next_cursors: dict[str, SubagentCursor] = {}
    for path in find_supplemental_transcripts_on_disk(SUBAGENT_SOURCE, transcript_path):
        prior = cursors.get(path)
        agent_id = Path(path).stem.removeprefix(_AGENT_PREFIX)
        try:
            stat = os.stat(path)
            cursor = prior if prior is not None and not _cursor_stale(prior, stat) else None
            start = cursor or SubagentCursor()
            lines, end = _complete_lines(path, start.offset)
        except OSError:
            logger.warning("Failed to read subagent transcript %s", path, exc_info=True)
            if prior is not None:
                next_cursors[path] = prior
            continue

        parser = get_parser(
            SUBAGENT_SOURCE,
            session_id=f"{session_id}:{agent_id}",
            transcript_path=path,
        )
        if start.parser_state:
            parser.hydrate_state(start.parser_state)
        records = (
            _parse_incremental_records(parser, lines, start_index=start.next_index) if lines else []
        )
        messages = [record for record in records if isinstance(record, ParsedMessage)]
        for message in messages:
            usage = message.usage
            if usage is None or not _has_tokens(usage):
                continue
            events.append(
                TokenEvent(
                    session_id=session_id,
                    project_id=project_id,
                    message_id=message.message_id,
                    source=SUBAGENT_SOURCE,
                    origin="transcript",
                    model=message.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_tokens=usage.cache_creation_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    context_window=None,
                    event_at=canonicalize_event_timestamp(message.timestamp),
                    metadata={"content_type": message.content_type, "agent_id": agent_id},
                    api_calls=api_call_count(SUBAGENT_SOURCE, message),
                )
            )
        next_cursors[path] = SubagentCursor(
            offset=end,
            next_index=messages[-1].index + 1 if messages else start.next_index,
            dev=stat.st_dev,
            ino=stat.st_ino,
            mtime_ns=stat.st_mtime_ns,
            parser_state=dict(parser.snapshot_state()),
        )
    return events, next_cursors


def _cursor_stale(cursor: SubagentCursor, stat: os.stat_result) -> bool:
    return (
        stat.st_size < cursor.offset
        or (stat.st_dev, stat.st_ino) != (cursor.dev, cursor.ino)
        or (cursor.mtime_ns is not None and stat.st_mtime_ns < cursor.mtime_ns)
    )


def _complete_lines(path: str, offset: int) -> tuple[list[str], int]:
    """Return the newline-terminated lines from ``offset`` and the offset past the last one."""
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    raw_lines = data[: end + 1].split(b"\n")[:-1]
    return [raw.decode("utf-8", errors="replace") + "\n" for raw in raw_lines], offset + end + 1


def _has_tokens(usage: TokenUsage) -> bool:
    return bool(
        usage.input_tokens
        or usage.output_tokens
        or usage.cache_creation_tokens
        or usage.cache_read_tokens
    )
