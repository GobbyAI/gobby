"""Incremental-resume snapshots for transcript evidence derivation."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import PydanticSerializationError

from gobby.tasks.transcript_evidence_cache import clear_snapshots, read_snapshot, write_snapshot
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptTaskClaim,
    TranscriptValidationRun,
)

logger = logging.getLogger(__name__)


@dataclass
class PendingTool:
    name: str
    arguments: dict[str, Any]
    timestamp: datetime
    order: int
    call_id: str | None = None
    #: The MCP server a provider reported beside a bare tool name (Codex).
    server: str | None = None


# --- Incremental derivation -------------------------------------------------
#
# Deriving evidence reads the whole transcript and reparses the whole claim
# window on every close attempt, so the cost is O(session length) even though
# the window's prefix never changes (#20876). Each derivation therefore leaves
# behind a per-session snapshot: everything the parse accumulated, pinned to a
# byte-offset watermark at the end of the last newline-terminated line. The
# next derivation with the same inputs seeks to the watermark, parses only the
# appended suffix, and continues from the carried state.
#
# The split is provably equivalent to one full parse because every coupling
# across the watermark travels with the snapshot: the parser's own cross-line
# state (`snapshot_state`/`hydrate_state` — Codex exec chains, Droid usage
# deltas; Claude/Qwen/Grok parse per line), the derivation's unresolved tool
# begins (`pending`), the event `order` counter, and the raw pre-dedup
# `degraded` list. The per-line window filter commutes with splitting the
# stream, and first-occurrence dedup of (dedup(prefix) + suffix) equals
# dedup(prefix + suffix).
#
# Measured on this repository's 92.6 MB / 59k-line session with a four-hour
# window: 589–611 ms for every full derivation before, 2 ms resuming over 40
# appended lines and under 1 ms over none — with identical evidence (79 runs,
# 35 edits) to a fresh full parse of the same file.
#
# A snapshot only ever *narrows* what is reparsed; it can never change what is
# derived. It is bypassed — and the whole file parsed, as before — whenever
# the derivation inputs' fingerprint differs, the transcript resolves to a
# different path (rotation into an archive), or the watermark no longer
# describes the file: shorter than the watermark (truncation), or different
# bytes at the watermark's tail (rewrite). The tail check and the suffix read
# share one file handle, so a rename-over between them cannot mix two files.
# A trailing line still missing its newline is parsed but never persisted
# beneath a watermark, so a mid-write race costs one full reparse, never a
# duplicated or dropped record.

_TAIL_CHECK_BYTES = 65_536
_SNAPSHOT_LIMIT = 8


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Derived evidence for one session transcript up to a byte watermark."""

    fingerprint: str
    transcript_path: str
    watermark: int
    tail_len: int
    tail_sha256: str
    parser_state: dict[str, Any]
    pending: dict[str, PendingTool]
    order: int
    runs: tuple[TranscriptValidationRun, ...]
    edits: tuple[TranscriptEdit, ...]
    degraded: tuple[str, ...]
    parsed_from_offset: int = 0
    latest_record_at: datetime | None = None
    claims: tuple[TranscriptTaskClaim, ...] = ()
    #: Per-path snapshots of the session's supplemental transcripts (Claude
    #: subagents), carried in the primary's record so one session keeps one
    #: cache slot. Subagent files are append-only, so after one full parse
    #: each later Stop reads only their new bytes.
    supplemental: dict[str, EvidenceSnapshot] = field(default_factory=dict)

    def checkpoint(self) -> tuple[object, ...]:
        """Identity of everything persisted, without comparing derived records."""
        return (
            self.fingerprint,
            self.watermark,
            self.tail_len,
            self.tail_sha256,
            tuple(sorted((path, s.checkpoint()) for path, s in self.supplemental.items())),
        )


_SNAPSHOT_ADAPTER = TypeAdapter(EvidenceSnapshot)


def _snapshot_offsets_valid(snapshot: EvidenceSnapshot) -> bool:
    return 0 <= snapshot.tail_len <= min(snapshot.watermark, _TAIL_CHECK_BYTES)


def load_durable_snapshot(session_id: str) -> EvidenceSnapshot | None:
    payload = read_snapshot(session_id)
    if payload is None:
        return None
    try:
        snapshot = _SNAPSHOT_ADAPTER.validate_json(payload)
        return snapshot if _snapshot_offsets_valid(snapshot) else None
    except (TypeError, ValueError):
        logger.debug("Ignoring invalid transcript evidence checkpoint", exc_info=True)
        return None


def store_durable_snapshot(session_id: str, snapshot: EvidenceSnapshot) -> None:
    try:
        write_snapshot(session_id, _SNAPSHOT_ADAPTER.dump_json(snapshot))
    except (PydanticSerializationError, TypeError, ValueError):
        logger.debug("Could not serialize transcript evidence checkpoint", exc_info=True)


@dataclass(frozen=True)
class TranscriptRead:
    """Decoded transcript lines plus the watermark bookkeeping behind them."""

    lines: list[str]
    watermark: int
    has_partial_tail: bool
    #: Bytes ending at ``watermark`` (at most ``_TAIL_CHECK_BYTES``), kept in
    #: memory so the stored checksum always describes the bytes that were
    #: parsed, not whatever a re-opened path holds by then.
    tail: bytes


_snapshot_lock = threading.Lock()
_evidence_snapshots: OrderedDict[str, EvidenceSnapshot] = OrderedDict()


def clear_evidence_snapshots() -> None:
    """Drop every cached per-session derivation (test isolation)."""
    with _snapshot_lock:
        _evidence_snapshots.clear()
    clear_snapshots()


def load_snapshot(session_id: str) -> EvidenceSnapshot | None:
    with _snapshot_lock:
        snapshot = _evidence_snapshots.get(session_id)
        if snapshot is not None:
            _evidence_snapshots.move_to_end(session_id)
        return snapshot


def store_snapshot(session_id: str, snapshot: EvidenceSnapshot) -> None:
    with _snapshot_lock:
        _evidence_snapshots[session_id] = snapshot
        _evidence_snapshots.move_to_end(session_id)
        while len(_evidence_snapshots) > _SNAPSHOT_LIMIT:
            _evidence_snapshots.popitem(last=False)


def _split_transcript_bytes(data: bytes, offset: int, prior_tail: bytes) -> TranscriptRead:
    """Decode ``data`` (the bytes at/after ``offset``) into positioned lines.

    ``prior_tail`` holds the bytes just before ``offset`` (empty for a full
    read); the returned :attr:`TranscriptRead.tail` is assembled from it and
    the newly read bytes so no re-read of the file is ever needed. Decoding is
    strict UTF-8, matching the text-mode read this replaced — a decode error
    propagates and becomes ``TranscriptEvidenceUnavailable``.
    """
    boundary = data.rfind(b"\n") + 1
    lines = [chunk.decode("utf-8") for chunk in data.splitlines(keepends=True)]
    watermark = offset + boundary
    combined = prior_tail + data[:boundary]
    tail_len = min(watermark, _TAIL_CHECK_BYTES)
    return TranscriptRead(
        lines=lines,
        watermark=watermark,
        has_partial_tail=boundary < len(data),
        tail=combined[len(combined) - tail_len :],
    )


def read_transcript(path: str) -> TranscriptRead:
    """Read a whole transcript for a full parse."""
    with open(path, "rb") as f:
        data = f.read()
    return _split_transcript_bytes(data, 0, b"")


def read_transcript_suffix(path: str, snapshot: EvidenceSnapshot) -> TranscriptRead | None:
    """Validate the snapshot's watermark and read the appended suffix.

    Returns ``None`` — full parse — when the file is shorter than the
    watermark, the bytes ending at the watermark no longer match, or the file
    cannot be read. Validation and the suffix read share one file handle so a
    concurrent rename-over cannot pass the check with one file and serve the
    suffix of another.
    """
    if not _snapshot_offsets_valid(snapshot):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() < snapshot.watermark:
                return None
            f.seek(snapshot.watermark - snapshot.tail_len)
            tail = f.read(snapshot.tail_len)
            if len(tail) != snapshot.tail_len:
                return None
            if hashlib.sha256(tail).hexdigest() != snapshot.tail_sha256:
                return None
            data = f.read()
    except OSError:
        return None
    return _split_transcript_bytes(data, snapshot.watermark, tail)


__all__ = [
    "EvidenceSnapshot",
    "PendingTool",
    "TranscriptRead",
    "clear_evidence_snapshots",
    "load_durable_snapshot",
    "load_snapshot",
    "read_transcript",
    "read_transcript_suffix",
    "store_durable_snapshot",
    "store_snapshot",
]
