"""Derive task-close validation evidence directly from provider transcripts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from gobby.config.validation_detection import (
    ValidationDetectionConfig,
    classify_validation_segments,
)
from gobby.sessions.machine_scope import require_local_session_ownership
from gobby.sessions.transcript_archive import get_archive_dir
from gobby.sessions.transcript_io import _iter_archive_lines
from gobby.sessions.transcript_paths import find_transcript_on_disk
from gobby.sessions.transcript_tool_metadata import extract_result_metadata
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import (
    ParsedMessage,
    ParsedToolEvent,
    RawLine,
    raw_lines_from_texts,
)
from gobby.storage.session_models import Session

logger = logging.getLogger(__name__)

EvidenceOutcome = Literal["success", "failure", "unknown"]

# How much transcript before the claim window still reaches the parser.
#
# Every consumer drops a record that falls outside the claim window, so lines
# older than the window can only cost parsing. Measured on this repository's
# 83 MB / 53k-line session, deriving against a claim made 30 minutes earlier:
# 420 ms handing the parser every line, 219 ms handing it only the window's.
# The saving is the parser's own per-line work, and it grows with session
# length while the window does not (#20866).
#
# Narrowing is exact for the records themselves — a differential run over that
# transcript produced identical runs, edits, and degraded capabilities for a
# 30-minute, a 7-hour, and a whole-session window. The lookback exists for the
# parser's cross-line state: a tool call whose begin and end straddle the
# boundary still needs its begin line. Two hours covers any realistic
# validation command, and a line whose timestamp is absent or not
# unambiguously UTC is always kept.
WINDOW_LOOKBACK = timedelta(hours=2)

_UTC_LINE_TIMESTAMP_RE = re.compile(
    r'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2}T[0-9:.]{8,})(?:Z|\+00:00)"'
)

_SHELL_TOOLS = {
    "bash",
    "exec_command",
    "execute",
    "execute_command",
    "run_command",
    "run_shell_command",
    "run_terminal_command",
    "shell",
    "terminal",
}
_EDIT_TOOLS = {"edit", "multiedit", "notebookedit", "write", "apply_patch", "exec"}
_COMMAND_KEYS = ("cmd", "command", "script")
_PATH_KEYS = ("file_path", "path", "notebook_path")
_EXIT_CODE_KEYS = ("exit_code", "exitCode")
_SUCCESS_STATUSES = {"completed", "ok", "passed", "success", "succeeded"}
_FAILURE_STATUSES = {"error", "failed", "failure"}
_OUTPUT_CHAR_LIMIT = 16_000

# Terminal summaries that prove the runner itself reported failures. Consulted only
# when the shell's aggregate status cannot speak for the matched segment; see
# `_extract_outcome`.
_RUNNER_FAILURE_PATTERNS = (
    # Counted summaries: pytest's "5 failed, 16 passed" and "178 errors in 4.90s",
    # vitest/jest's "Tests  3 failed | 5 passed", mypy's "Found 2 errors in 1 file",
    # ruff's "Found 3 errors.". The [1-9] guard keeps "0 failed" and "0 errors" out,
    # and requiring the word immediately after the count keeps "1 xfailed" out.
    # Count and word must share a line ([^\S\n], not \s): every counted summary
    # puts them on one, and letting the gap span a newline misread a PASSING
    # `gobby test-types audit` ratchet — "Files scanned: 2\nErrors: 10" (ten
    # baselined errors, zero new) — as "2 errors" (#20880).
    re.compile(r"\b[1-9]\d*[^\S\n]+(?:failed|failures?|errors?)\b", re.IGNORECASE),
    # Per-test failure lines: pytest's "FAILED path::test" and "ERROR path::test",
    # go's "--- FAIL: TestX" and "FAIL\tpkg\t0.1s", cargo's "test result: FAILED.".
    re.compile(
        r"(?m)^\s*(?:FAILED\s+\S|ERROR\s+\S+::|---\s+FAIL:|FAIL\s+\S|test result:\s*FAILED\b)"
    ),
    # Gobby audit ratchets (`gobby test-types audit`, `gobby test-quality audit`):
    # a passing run still headlines the TOTAL count of baselined findings
    # ("Errors: 10"), so only the ratchet's own failing tally proves failure.
    re.compile(r"(?m)^\s*Failing new (?:errors|issues) >= \w+: [1-9]\d*\b"),
)


@dataclass(frozen=True)
class TranscriptValidationRun:
    """One transcript-backed validation command outcome."""

    session_id: str
    source: str
    command: str
    categories: tuple[str, ...]
    matcher_id: str
    label: str
    outcome: EvidenceOutcome
    started_at: datetime
    completed_at: datetime
    order: int
    exit_code: int | None = None
    unknown_reason: str | None = None
    output: str | None = None
    output_truncated: bool = False
    #: Normalized argv of every validation segment of ``command`` (wrappers and
    #: env assignments stripped); empty only for runs built without classification.
    validation_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class TranscriptEdit:
    """One task-attributed edit observed in a transcript."""

    session_id: str
    source: str
    path: str
    timestamp: datetime
    order: int
    tool_name: str


@dataclass(frozen=True)
class TranscriptEvidence:
    """Validation runs and task edits derived from one or more sessions."""

    validation_runs: tuple[TranscriptValidationRun, ...] = ()
    edits: tuple[TranscriptEdit, ...] = ()
    attempted_paths: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    degraded_capabilities: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        """Return bounded deterministic facts for checklist diagnostics."""
        outcome_counts = {"success": 0, "failure": 0, "unknown": 0}
        category_successes: dict[str, int] = {}
        for run in self.validation_runs:
            outcome_counts[run.outcome] += 1
            if run.outcome == "success":
                for category in run.categories:
                    category_successes[category] = category_successes.get(category, 0) + 1
        return {
            "sessions": list(self.sessions),
            "validation_run_count": len(self.validation_runs),
            "outcomes": outcome_counts,
            "successful_categories": category_successes,
            "task_edit_count": len(self.edits),
            "latest_task_edit_at": (
                max(edit.timestamp for edit in self.edits).isoformat() if self.edits else None
            ),
            "degraded_capabilities": list(self.degraded_capabilities),
        }


class TranscriptEvidenceUnavailable(RuntimeError):
    """Raised when no readable transcript exists for a required session."""

    def __init__(
        self,
        message: str,
        *,
        source: str,
        attempted_paths: Iterable[str],
    ) -> None:
        super().__init__(message)
        self.source = source
        self.attempted_paths = tuple(dict.fromkeys(attempted_paths))
        self.retry_after = 5


@dataclass
class _PendingTool:
    name: str
    arguments: dict[str, Any]
    timestamp: datetime
    order: int


@dataclass
class _DerivationState:
    session: Session
    detection_config: ValidationDetectionConfig
    task_edited_files: set[str]
    repo_path: str
    window_start: datetime | None
    pending: dict[str, _PendingTool] = field(default_factory=dict)
    runs: list[TranscriptValidationRun] = field(default_factory=list)
    edits: list[TranscriptEdit] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    order: int = 0

    def next_order(self) -> int:
        self.order += 1
        return self.order


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
class _EvidenceSnapshot:
    """Derived evidence for one session transcript up to a byte watermark."""

    fingerprint: str
    transcript_path: str
    watermark: int
    tail_len: int
    tail_sha256: str
    parser_state: dict[str, Any]
    pending: dict[str, _PendingTool]
    order: int
    runs: tuple[TranscriptValidationRun, ...]
    edits: tuple[TranscriptEdit, ...]
    degraded: tuple[str, ...]


@dataclass(frozen=True)
class _TranscriptRead:
    """Decoded transcript lines plus the watermark bookkeeping behind them."""

    lines: list[str]
    watermark: int
    has_partial_tail: bool
    #: Bytes ending at ``watermark`` (at most ``_TAIL_CHECK_BYTES``), kept in
    #: memory so the stored checksum always describes the bytes that were
    #: parsed, not whatever a re-opened path holds by then.
    tail: bytes


_snapshot_lock = threading.Lock()
_evidence_snapshots: OrderedDict[str, _EvidenceSnapshot] = OrderedDict()


def clear_evidence_snapshots() -> None:
    """Drop every cached per-session derivation (test isolation)."""
    with _snapshot_lock:
        _evidence_snapshots.clear()


def _load_snapshot(session_id: str) -> _EvidenceSnapshot | None:
    with _snapshot_lock:
        snapshot = _evidence_snapshots.get(session_id)
        if snapshot is not None:
            _evidence_snapshots.move_to_end(session_id)
        return snapshot


def _store_snapshot(session_id: str, snapshot: _EvidenceSnapshot) -> None:
    with _snapshot_lock:
        _evidence_snapshots[session_id] = snapshot
        _evidence_snapshots.move_to_end(session_id)
        while len(_evidence_snapshots) > _SNAPSHOT_LIMIT:
            _evidence_snapshots.popitem(last=False)


def _derivation_fingerprint(
    session: Session,
    window_start: datetime | None,
    detection_config: ValidationDetectionConfig,
    task_edited_files: set[str],
    repo_path: str,
) -> str:
    """Fingerprint every input the derived records are a function of."""
    payload = json.dumps(
        {
            "session": session.id,
            "source": session.source,
            "window_start": window_start.isoformat() if window_start is not None else None,
            "repo_path": repo_path,
            "task_edited_files": sorted(task_edited_files),
            "detection": detection_config.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _split_transcript_bytes(data: bytes, offset: int, prior_tail: bytes) -> _TranscriptRead:
    """Decode ``data`` (the bytes at/after ``offset``) into positioned lines.

    ``prior_tail`` holds the bytes just before ``offset`` (empty for a full
    read); the returned :attr:`_TranscriptRead.tail` is assembled from it and
    the newly read bytes so no re-read of the file is ever needed. Decoding is
    strict UTF-8, matching the text-mode read this replaced — a decode error
    propagates and becomes ``TranscriptEvidenceUnavailable``.
    """
    boundary = data.rfind(b"\n") + 1
    lines = [chunk.decode("utf-8") for chunk in data.splitlines(keepends=True)]
    watermark = offset + boundary
    combined = prior_tail + data[:boundary]
    tail_len = min(watermark, _TAIL_CHECK_BYTES)
    return _TranscriptRead(
        lines=lines,
        watermark=watermark,
        has_partial_tail=boundary < len(data),
        tail=combined[len(combined) - tail_len :],
    )


def _read_transcript(path: str) -> _TranscriptRead:
    """Read a whole transcript for a full parse."""
    with open(path, "rb") as f:
        data = f.read()
    return _split_transcript_bytes(data, 0, b"")


def _read_transcript_suffix(path: str, snapshot: _EvidenceSnapshot) -> _TranscriptRead | None:
    """Validate the snapshot's watermark and read the appended suffix.

    Returns ``None`` — full parse — when the file is shorter than the
    watermark, the bytes ending at the watermark no longer match, or the file
    cannot be read. Validation and the suffix read share one file handle so a
    concurrent rename-over cannot pass the check with one file and serve the
    suffix of another.
    """
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


async def derive_transcript_evidence(
    session: Session,
    window_start: datetime | str | None,
    detection_config: ValidationDetectionConfig,
    task_edited_files: set[str],
    repo_path: str,
    *,
    archive_dir: str | None = None,
) -> TranscriptEvidence:
    """Parse a complete provider transcript and derive close-checklist evidence."""
    return await asyncio.to_thread(
        _derive_transcript_evidence_sync,
        session,
        _coerce_datetime(window_start),
        detection_config,
        set(task_edited_files),
        repo_path,
        archive_dir,
    )


def select_window_raw_lines(
    lines: Iterable[str],
    window_start: datetime | None,
) -> Iterator[RawLine]:
    """Yield raw lines the claim window can still reach, keeping line numbers.

    A line is dropped only when it carries an unambiguously UTC timestamp
    older than :data:`WINDOW_LOOKBACK` before the window. Everything else —
    later lines, undated lines, and timestamps in another offset — is kept, so
    this narrows the parser's input without deciding anything about it.
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
            continue
        yield RawLine(byte_offset=None, raw_line_no=index, text=text)


def merge_transcript_evidence(*evidence_sets: TranscriptEvidence) -> TranscriptEvidence:
    """Merge session evidence while preserving provider timestamps and local order."""
    runs = sorted(
        (run for evidence in evidence_sets for run in evidence.validation_runs),
        key=lambda run: (run.completed_at, run.session_id, run.order),
    )
    edits = sorted(
        (edit for evidence in evidence_sets for edit in evidence.edits),
        key=lambda edit: (edit.timestamp, edit.session_id, edit.order),
    )
    return TranscriptEvidence(
        validation_runs=tuple(runs),
        edits=tuple(edits),
        attempted_paths=tuple(
            dict.fromkeys(path for evidence in evidence_sets for path in evidence.attempted_paths)
        ),
        sessions=tuple(
            dict.fromkeys(session for evidence in evidence_sets for session in evidence.sessions)
        ),
        degraded_capabilities=tuple(
            dict.fromkeys(
                item for evidence in evidence_sets for item in evidence.degraded_capabilities
            )
        ),
    )


def _derive_transcript_evidence_sync(
    session: Session,
    window_start: datetime | None,
    detection_config: ValidationDetectionConfig,
    task_edited_files: set[str],
    repo_path: str,
    archive_dir: str | None,
) -> TranscriptEvidence:
    path, attempted_paths = _resolve_transcript_path(session, archive_dir)
    if path is None:
        raise TranscriptEvidenceUnavailable(
            f"No transcript was found for {session.source} session {session.ref}.",
            source=session.source,
            attempted_paths=attempted_paths,
        )

    normalized_task_files = {_normalize_known_path(item, repo_path) for item in task_edited_files}
    fingerprint = _derivation_fingerprint(
        session, window_start, detection_config, normalized_task_files, repo_path
    )
    resume = _load_snapshot(session.id)
    if resume is not None and (
        resume.fingerprint != fingerprint or resume.transcript_path != path or path.endswith(".gz")
    ):
        resume = None

    read: _TranscriptRead | None = None
    try:
        if path.endswith(".gz"):
            lines = list(_iter_archive_lines(path))
        else:
            if resume is not None:
                read = _read_transcript_suffix(path, resume)
            if read is None:
                resume = None
                read = _read_transcript(path)
            lines = read.lines
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise TranscriptEvidenceUnavailable(
            f"Transcript {path} could not be read: {exc}",
            source=session.source,
            attempted_paths=attempted_paths,
        ) from exc

    try:
        parser = get_parser(session.source, session_id=session.id, transcript_path=path)
    except ValueError as exc:
        raise TranscriptEvidenceUnavailable(
            str(exc),
            source=session.source,
            attempted_paths=attempted_paths,
        ) from exc

    state = _DerivationState(
        session=session,
        detection_config=detection_config,
        task_edited_files=normalized_task_files,
        repo_path=repo_path,
        window_start=window_start,
    )
    if resume is not None:
        # Deep-copy on load: hydrated parsers and the pending map may share
        # containers with the live parse, and the cached snapshot must stay
        # exactly what the previous derivation stored.
        parser.hydrate_state(deepcopy(resume.parser_state))
        state.pending = deepcopy(resume.pending)
        state.runs = list(resume.runs)
        state.edits = list(resume.edits)
        state.degraded = list(resume.degraded)
        state.order = resume.order

    for event in parser.iter_parse_events(select_window_raw_lines(lines, window_start)):
        for outcome in event.codex_exec_outcomes:
            _consume_codex_outcome(state, outcome)
        for record in event.records:
            if isinstance(record, ParsedMessage):
                _consume_message(state, record)
            elif isinstance(record, ParsedToolEvent):
                _consume_tool_event(state, record)

    if read is not None and not read.has_partial_tail:
        _store_snapshot(
            session.id,
            _EvidenceSnapshot(
                fingerprint=fingerprint,
                transcript_path=path,
                watermark=read.watermark,
                tail_len=len(read.tail),
                tail_sha256=hashlib.sha256(read.tail).hexdigest(),
                parser_state=parser.snapshot_state(),
                pending=dict(state.pending),
                order=state.order,
                runs=tuple(state.runs),
                edits=tuple(state.edits),
                degraded=tuple(state.degraded),
            ),
        )
    logger.debug(
        "Derived close transcript evidence",
        extra={
            "session_id": session.id,
            "transcript_path": path,
            "resumed": resume is not None,
            "parsed_lines": len(lines),
            "watermark": read.watermark if read is not None else None,
        },
    )

    return TranscriptEvidence(
        validation_runs=tuple(state.runs),
        edits=tuple(state.edits),
        attempted_paths=tuple(attempted_paths),
        sessions=(session.id,),
        degraded_capabilities=tuple(dict.fromkeys(state.degraded)),
    )


def _resolve_transcript_path(
    session: Session,
    archive_dir: str | None,
) -> tuple[str | None, list[str]]:
    local_machine_id = require_local_session_ownership(session)
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


def _consume_message(state: _DerivationState, message: ParsedMessage) -> None:
    timestamp = _as_utc(message.timestamp)
    if not _inside_window(timestamp, state.window_start):
        return
    order = state.next_order()
    call_id = message.tool_use_id
    if message.content_type == "tool_use":
        name = message.tool_name or ""
        arguments = message.tool_input or {}
        if call_id:
            state.pending[call_id] = _PendingTool(name, arguments, timestamp, order)
        _record_edit(state, name, arguments, timestamp, order)
        return
    if message.content_type != "tool_result" or not call_id:
        return
    pending = state.pending.pop(call_id, None)
    if pending is None:
        return
    _record_validation_run(
        state,
        pending,
        result={"tool_result": message.tool_result, "raw_json": message.raw_json},
        completed_at=timestamp,
        order=order,
        source_label=f"{state.session.source} tool result",
    )


def _consume_tool_event(state: _DerivationState, event: ParsedToolEvent) -> None:
    timestamp = _as_utc(event.timestamp)
    if not _inside_window(timestamp, state.window_start):
        return
    order = state.next_order()
    call_id = event.call_id
    if event.phase == "begin":
        name = event.tool or ""
        if call_id:
            state.pending[call_id] = _PendingTool(name, event.arguments, timestamp, order)
        _record_edit(state, name, event.arguments, timestamp, order)
        return
    if event.phase != "end" or not call_id:
        return
    pending = state.pending.pop(call_id, None)
    if pending is None:
        return
    result = event.result
    if event.error is not None:
        result = {"success": False, "error": event.error, "result": result}
    _record_validation_run(
        state,
        pending,
        result=result,
        completed_at=timestamp,
        order=order,
        source_label=f"{state.session.source} direct tool event",
    )


def _consume_codex_outcome(state: _DerivationState, outcome: Any) -> None:
    completed_at = _as_utc(outcome.timestamp)
    if not _inside_window(completed_at, state.window_start):
        return
    pending = state.pending.get(outcome.outer_call_id)
    direct_pending = pending is not None and _tool_basename(pending.name) != "exec"
    order = state.next_order()
    matches = classify_validation_segments(outcome.command, state.detection_config)
    if not matches:
        return
    match = matches[0]
    output, output_truncated = _extract_output(outcome.result)
    status, exit_code, unknown_reason = _extract_outcome(
        outcome.result,
        output,
        aggregate_status_is_trustworthy=not match.is_compound,
    )
    if direct_pending:
        if status == "unknown":
            # Keep the call pending so ParsedMessage can recover structured
            # direct results that the execution-chain parser cannot certify.
            return
        # The execution-chain parser accepts direct terminal outcomes only from
        # the exact native envelope. Consume the call before ParsedMessage sees
        # the same function_call_output and records a duplicate result.
        state.pending.pop(outcome.outer_call_id, None)
    if status == "unknown":
        state.degraded.append(
            f"codex could not recover a definitive outcome for {match.label}: "
            f"{unknown_reason or 'unknown result'}"
        )
    state.runs.append(
        TranscriptValidationRun(
            session_id=state.session.id,
            source=state.session.source,
            command=outcome.command,
            categories=match.categories,
            matcher_id=match.matcher_id,
            label=match.label,
            outcome=status,
            started_at=completed_at,
            completed_at=completed_at,
            order=order,
            exit_code=exit_code,
            unknown_reason=unknown_reason,
            output=output,
            output_truncated=output_truncated,
            validation_commands=tuple(
                dict.fromkeys(segment.normalized_command for segment in matches)
            ),
        )
    )


def _record_validation_run(
    state: _DerivationState,
    pending: _PendingTool,
    *,
    result: Any,
    completed_at: datetime,
    order: int,
    source_label: str,
) -> None:
    if _tool_basename(pending.name) not in _SHELL_TOOLS:
        return
    command = _extract_command(pending.arguments)
    matches = classify_validation_segments(command, state.detection_config)
    if not matches:
        return
    match = matches[0]
    output, output_truncated = _extract_output(result)
    outcome, exit_code, unknown_reason = _extract_outcome(
        result,
        output,
        aggregate_status_is_trustworthy=not match.is_compound,
    )
    if outcome == "unknown":
        state.degraded.append(
            f"{source_label} lacks a definitive exit outcome for {match.label}; "
            "re-run the command in a supported shell tool"
        )
    state.runs.append(
        TranscriptValidationRun(
            session_id=state.session.id,
            source=state.session.source,
            command=command,
            categories=match.categories,
            matcher_id=match.matcher_id,
            label=match.label,
            outcome=outcome,
            started_at=pending.timestamp,
            completed_at=completed_at,
            order=order,
            exit_code=exit_code,
            unknown_reason=unknown_reason,
            output=output,
            output_truncated=output_truncated,
            validation_commands=tuple(
                dict.fromkeys(segment.normalized_command for segment in matches)
            ),
        )
    )


def _extract_output(result: Any) -> tuple[str | None, bool]:
    """Extract bounded command output needed to classify validation failures."""
    parts: list[str] = []
    seen: set[str] = set()
    for value in _walk_values(result):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    if not parts:
        return None, False
    output = "\n".join(parts)
    if len(output) <= _OUTPUT_CHAR_LIMIT:
        return output, False
    half = (_OUTPUT_CHAR_LIMIT - len("\n...[output truncated]...\n")) // 2
    return (
        f"{output[:half]}\n...[output truncated]...\n{output[-half:]}",
        True,
    )


def _record_edit(
    state: _DerivationState,
    tool_name: str,
    arguments: dict[str, Any],
    timestamp: datetime,
    order: int,
) -> None:
    basename = _tool_basename(tool_name)
    if basename not in _EDIT_TOOLS:
        return
    paths = _extract_edit_paths(basename, arguments, state.repo_path)
    for path in paths:
        if path not in state.task_edited_files:
            continue
        state.edits.append(
            TranscriptEdit(
                session_id=state.session.id,
                source=state.session.source,
                path=path,
                timestamp=timestamp,
                order=order,
                tool_name=tool_name,
            )
        )


def _extract_edit_paths(
    tool_name: str,
    arguments: dict[str, Any],
    repo_path: str,
) -> set[str]:
    values: set[str] = set()
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            values.add(_normalize_known_path(value, repo_path))
    if tool_name in {"apply_patch", "exec"}:
        raw = arguments.get("raw") or arguments.get("patch") or arguments.get("input")
        if isinstance(raw, str):
            if tool_name == "exec":
                if "tools.apply_patch" not in raw:
                    return values
                raw = raw.replace(r"\r", "\r").replace(r"\n", "\n")
            for line in raw.splitlines():
                for prefix in ("*** Add File: ", "*** Delete File: ", "*** Update File: "):
                    if line.startswith(prefix):
                        values.add(_normalize_known_path(line.removeprefix(prefix), repo_path))
    return values


def _extract_command(arguments: dict[str, Any]) -> str:
    for key in _COMMAND_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_outcome(
    result: Any,
    output: str | None = None,
    *,
    aggregate_status_is_trustworthy: bool = True,
) -> tuple[EvidenceOutcome, int | None, str | None]:
    """Classify one shell result as a validation pass, failure, or unknown.

    A shell reports the status of the LAST element of a list or pipeline, so
    ``pytest ... | tail``, ``pytest ... ; echo``, and ``pytest ... && other`` all
    record a zero status for a genuinely failing run — which is how a real red
    gets filed as a pass. Pass ``aggregate_status_is_trustworthy=False`` for a
    compound match (``ValidationCommandMatch.is_compound``); the runner's own
    terminal summary then decides, because the aggregate status cannot prove the
    matched segment passed. A non-compound run keeps its recorded status verbatim.
    """
    if not aggregate_status_is_trustworthy and _runner_reported_failures(output):
        return "failure", _find_exit_code(result), None

    exit_code = _find_exit_code(result)
    if exit_code is not None:
        return ("success" if exit_code == 0 else "failure"), exit_code, None

    values = list(_walk_values(result))
    for value in values:
        if not isinstance(value, dict):
            continue
        success = value.get("success")
        if isinstance(success, bool):
            return ("success" if success else "failure"), None, None
        status = value.get("status")
        if isinstance(status, str):
            normalized = status.strip().casefold()
            if normalized in _SUCCESS_STATUSES:
                return "success", None, None
            if normalized in _FAILURE_STATUSES:
                return "failure", None, None
        is_error = value.get("is_error")
        if isinstance(is_error, bool):
            return ("failure" if is_error else "success"), None, None
        error = value.get("error")
        if error not in (None, "", False, []):
            return "failure", None, None

    unknown_reason = None
    for value in values:
        if isinstance(value, dict):
            reason = value.get("unknown_reason")
            if isinstance(reason, str) and reason:
                unknown_reason = reason
                break
    return "unknown", None, unknown_reason or "missing definitive provider outcome"


def _runner_reported_failures(output: str | None) -> bool:
    """Return whether command output carries a runner's own failure summary."""
    if not output:
        return False
    return any(pattern.search(output) for pattern in _RUNNER_FAILURE_PATTERNS)


def _find_exit_code(result: Any) -> int | None:
    for value in _walk_values(result):
        if not isinstance(value, dict):
            continue
        metadata = extract_result_metadata("bash", value)
        candidates = [metadata.get("exit_code"), *(value.get(key) for key in _EXIT_CODE_KEYS)]
        for candidate in candidates:
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
    return None


def _walk_values(value: Any, *, depth: int = 0) -> Iterable[Any]:
    if depth > 8:
        return
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk_values(nested, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_values(nested, depth=depth + 1)
    elif isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _walk_values(decoded, depth=depth + 1)


def _tool_basename(tool_name: str) -> str:
    normalized = tool_name.casefold().replace("-", "_")
    for separator in ("__", ".", "/"):
        if separator in normalized:
            normalized = normalized.rsplit(separator, 1)[-1]
    return normalized


def _normalize_known_path(path: str, repo_path: str) -> str:
    normalized = os.path.normpath(path)
    if os.path.isabs(normalized):
        try:
            normalized = os.path.relpath(normalized, repo_path)
        except ValueError:
            pass
    while normalized.startswith(f".{os.sep}"):
        normalized = normalized[2:]
    return normalized.replace(os.sep, "/")


def _inside_window(timestamp: datetime, window_start: datetime | None) -> bool:
    return window_start is None or timestamp >= window_start


def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "TranscriptEdit",
    "TranscriptEvidence",
    "TranscriptEvidenceUnavailable",
    "TranscriptValidationRun",
    "clear_evidence_snapshots",
    "derive_transcript_evidence",
    "merge_transcript_evidence",
]
