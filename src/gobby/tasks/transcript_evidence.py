"""Derive task-close validation evidence directly from provider transcripts."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from gobby.config.validation_detection import (
    ValidationCommandMatch,
    ValidationDetectionConfig,
    classify_validation_segments,
)
from gobby.hooks.normalization import (
    _normalize_shell_tool_metadata as _shell_tool_metadata,
)
from gobby.sessions.machine_scope import require_local_session_ownership
from gobby.sessions.transcript_archive import get_archive_dir
from gobby.sessions.transcript_io import _iter_archive_lines
from gobby.sessions.transcript_paths import (
    find_supplemental_transcripts_on_disk,
    find_transcript_on_disk,
)
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import (
    ParsedMessage,
    ParsedToolEvent,
    RawLine,
    raw_lines_from_texts,
)
from gobby.storage.session_models import Session
from gobby.tasks.transcript_background import pending_background_run, recover_background_receipt
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
    TranscriptTaskClaim,
    TranscriptValidationRun,
    TranscriptValidationSegment,
)
from gobby.tasks.transcript_evidence_pool import run_in_transcript_evidence_pool
from gobby.tasks.transcript_evidence_snapshots import (
    EvidenceSnapshot,
    PendingTool,
    TranscriptRead,
    load_durable_snapshot,
    read_transcript,
    read_transcript_suffix,
    store_durable_snapshot,
)
from gobby.tasks.transcript_evidence_transfer import (
    ChunkedPayload,
    decode_cooperatively,
    encode,
)
from gobby.tasks.transcript_outcomes import (
    classify_validation_command_equivalence,
)
from gobby.tasks.transcript_outcomes import (
    extract_outcome as _extract_outcome,
)
from gobby.tasks.transcript_outcomes import (
    extract_output as _extract_output,
)
from gobby.tasks.transcript_outcomes import (
    is_unexecuted_tool_result as _is_unexecuted_tool_result,
)
from gobby.tasks.transcript_output_retention import (
    _RTK_RECALL_RE,
    _drop_settled_command_output,
    _retained_output,
)
from gobby.tasks.transcript_task_claims import codex_item_claim, task_claim
from gobby.tasks.transcript_tool_arguments import (
    edited_source,
    python_added_source,
    python_edit_tokens,
    python_keyword_stub,
    tool_workdir,
)
from gobby.tasks.transcript_tool_arguments import (
    extract_command as _extract_command,
)
from gobby.tasks.transcript_tool_arguments import (
    extract_edit_paths as _extract_edit_paths,
)
from gobby.tasks.transcript_tool_arguments import (
    match_task_file as _match_task_file,
)
from gobby.tasks.transcript_tool_arguments import (
    normalize_known_path as _normalize_known_path,
)
from gobby.tasks.transcript_tool_arguments import (
    normalize_tool_name as _tool_basename,
)
from gobby.tasks.transcript_tool_arguments import resolve_edit_path as _resolve_edit_path

logger = logging.getLogger(__name__)

# How much transcript before the claim window still reaches the parser.
# Lines older than the claim window only cost parsing. On an 83 MB / 53k-line
# session, filtering cut derivation from 420 ms to 219 ms (#20866).
#
# Narrowing is exact for the records themselves — a differential run over that
# transcript produced identical evidence for 30-minute, 7-hour, and full-session
# windows. The lookback preserves cross-line parser state for tool calls that
# straddle the boundary; absent or non-UTC timestamps are always kept.
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
_EDIT_TOOLS = {
    "edit",
    "multiedit",
    "notebookedit",
    "write",
    "apply_patch",
    "exec",
    "search_replace",
}


@dataclass
class _DerivationState:
    session: Session
    detection_config: ValidationDetectionConfig
    task_edited_files: set[str]
    repo_path: str
    task_checkout_paths: frozenset[tuple[str, str]] | None
    window_start: datetime | None
    pending: dict[str, PendingTool] = field(default_factory=dict)
    runs: list[TranscriptValidationRun] = field(default_factory=list)
    edits: list[TranscriptEdit] = field(default_factory=list)
    claims: list[TranscriptTaskClaim] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    order: int = 0
    latest_record_at: datetime | None = None

    def next_order(self) -> int:
        self.order += 1
        return self.order


def _derivation_fingerprint(
    session: Session,
    window_start: datetime | None,
    detection_config: ValidationDetectionConfig,
    task_edited_files: set[str],
    repo_path: str,
    task_checkout_paths: frozenset[tuple[str, str]] | None,
) -> str:
    """Fingerprint every input the derived records are a function of."""
    payload = json.dumps(
        {
            "derivation_version": 16,
            "session": session.id,
            "source": session.source,
            "window_start": window_start.isoformat() if window_start is not None else None,
            "repo_path": repo_path,
            "task_checkout_paths": sorted(task_checkout_paths)
            if task_checkout_paths is not None
            else None,
            "task_edited_files": sorted(task_edited_files),
            "detection": detection_config.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def derive_transcript_evidence(
    session: Session,
    window_start: datetime | str | None,
    detection_config: ValidationDetectionConfig,
    task_edited_files: set[str],
    repo_path: str,
    *,
    archive_dir: str | None = None,
    task_checkout_paths: frozenset[tuple[str, str]] | None = None,
) -> TranscriptEvidence:
    """Parse a complete provider transcript and derive close-checklist evidence."""
    local_machine_id = require_local_session_ownership(session)
    payload = await run_in_transcript_evidence_pool(
        _derive_chunked_transcript_evidence,
        session,
        _coerce_datetime(window_start),
        detection_config,
        set(task_edited_files),
        repo_path,
        task_checkout_paths,
        archive_dir,
        local_machine_id,
    )
    return cast(TranscriptEvidence, await decode_cooperatively(payload))


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
    """Merge session evidence into one transcript position sequence.

    Each session numbers ``order`` from its own counter, so raw values are not
    comparable across the owner and closing sessions the close path merges.
    Sessions interleave by provider timestamp without reordering within a
    session, and the merged edits and runs are renumbered as one sequence so
    every consumer can compare positions.
    """
    streams: list[list[tuple[datetime, TranscriptValidationRun | TranscriptEdit]]] = []
    for evidence in evidence_sets:
        items: list[tuple[datetime, TranscriptValidationRun | TranscriptEdit]] = [
            (run.completed_at, run) for run in (*evidence.validation_runs, *evidence.command_runs)
        ]
        items.extend((edit.timestamp, edit) for edit in evidence.edits)
        items.sort(key=lambda item: item[1].order)
        if items:
            streams.append(items)

    cursors = [0] * len(streams)
    runs: list[TranscriptValidationRun] = []
    edits: list[TranscriptEdit] = []
    position = 0
    while True:
        heads = [
            (streams[index][cursor][0], index)
            for index, cursor in enumerate(cursors)
            if cursor < len(streams[index])
        ]
        if not heads:
            break
        _stamp, index = min(heads)
        item = streams[index][cursors[index]][1]
        cursors[index] += 1
        position += 1
        if isinstance(item, TranscriptValidationRun):
            runs.append(replace(item, order=position))
        else:
            edits.append(replace(item, order=position))

    return TranscriptEvidence(
        validation_runs=tuple(run for run in runs if run.categories),
        command_runs=tuple(run for run in runs if not run.categories),
        excluded_runs=tuple(run for evidence in evidence_sets for run in evidence.excluded_runs),
        edits=tuple(edits),
        task_claims=tuple(claim for evidence in evidence_sets for claim in evidence.task_claims),
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
        latest_record_at=max(
            (
                evidence.latest_record_at
                for evidence in evidence_sets
                if evidence.latest_record_at is not None
            ),
            default=None,
        ),
    )


def _derive_chunked_transcript_evidence(
    session: Session,
    window_start: datetime | None,
    detection_config: ValidationDetectionConfig,
    task_edited_files: set[str],
    repo_path: str,
    task_checkout_paths: frozenset[tuple[str, str]] | None,
    archive_dir: str | None,
    local_machine_id: str,
) -> ChunkedPayload:
    """Pool entry: records cross the boundary in chunks the event loop decodes."""
    return encode(
        _derive_transcript_evidence_sync(
            session,
            window_start,
            detection_config,
            task_edited_files,
            repo_path,
            task_checkout_paths,
            archive_dir,
            local_machine_id,
            None,
        )[0]
    )


def _derive_transcript_evidence_sync(
    session: Session,
    window_start: datetime | None,
    detection_config: ValidationDetectionConfig,
    task_edited_files: set[str],
    repo_path: str,
    task_checkout_paths: frozenset[tuple[str, str]] | None,
    archive_dir: str | None,
    local_machine_id: str,
    resume: EvidenceSnapshot | None,
) -> tuple[TranscriptEvidence, EvidenceSnapshot | None]:
    fingerprint = _derivation_fingerprint(
        session,
        window_start,
        detection_config,
        {_normalize_known_path(item, repo_path) for item in task_edited_files},
        repo_path,
        task_checkout_paths,
    )
    snapshot_key = f"{session.id}:{fingerprint}"
    if resume is None:
        resume = load_durable_snapshot(snapshot_key)
    paths, attempted_paths = _resolve_transcript_paths(session, archive_dir, local_machine_id)
    if not paths:
        raise TranscriptEvidenceUnavailable(
            f"No transcript was found for {session.source} session {session.ref}.",
            source=session.source,
            attempted_paths=attempted_paths,
        )
    results = tuple(
        _derive_transcript_path_evidence(
            session,
            path,
            window_start,
            detection_config,
            task_edited_files,
            repo_path,
            task_checkout_paths,
            attempted_paths,
            resume=(
                resume
                if index == 0
                else resume.supplemental.get(path)
                if resume is not None
                else None
            ),
        )
        for index, path in enumerate(paths)
    )
    updated = results[0][1]
    if updated is not None:
        updated = replace(
            updated,
            supplemental={
                path: snapshot
                for path, (_, snapshot) in zip(paths[1:], results[1:], strict=True)
                if snapshot is not None
            },
        )
        if resume is None or updated.checkpoint() != resume.checkpoint():
            store_durable_snapshot(snapshot_key, updated)
    return merge_transcript_evidence(*(result[0] for result in results)), updated


def _derive_transcript_path_evidence(
    session: Session,
    path: str,
    window_start: datetime | None,
    detection_config: ValidationDetectionConfig,
    task_edited_files: set[str],
    repo_path: str,
    task_checkout_paths: frozenset[tuple[str, str]] | None,
    attempted_paths: list[str],
    *,
    resume: EvidenceSnapshot | None,
) -> tuple[TranscriptEvidence, EvidenceSnapshot | None]:
    normalized_task_files = {_normalize_known_path(item, repo_path) for item in task_edited_files}
    fingerprint = _derivation_fingerprint(
        session,
        window_start,
        detection_config,
        normalized_task_files,
        repo_path,
        task_checkout_paths,
    )
    if resume is not None and (
        resume.fingerprint != fingerprint or resume.transcript_path != path or path.endswith(".gz")
    ):
        resume = None

    read: TranscriptRead | None = None
    try:
        if path.endswith(".gz"):
            lines = list(_iter_archive_lines(path))
        else:
            if resume is not None:
                read = read_transcript_suffix(path, resume)
            if read is None:
                resume = None
                read = read_transcript(path)
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
        task_checkout_paths=task_checkout_paths,
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
        state.claims = list(resume.claims)
        state.degraded = list(resume.degraded)
        state.order = resume.order
        state.latest_record_at = resume.latest_record_at

    for event in parser.iter_parse_events(select_window_raw_lines(lines, window_start)):
        for outcome in event.codex_exec_outcomes:
            _consume_codex_outcome(state, outcome)
        for item in event.codex_mcp_calls:
            if claim := codex_item_claim(item, state.window_start, state.pending):
                state.claims.append(claim)
        for record in event.records:
            if isinstance(record, ParsedMessage):
                _observe_record_time(state, record.timestamp)
                _consume_message(state, record)
            elif isinstance(record, ParsedToolEvent):
                _observe_record_time(state, record.timestamp)
                _consume_tool_event(state, record)
    state.runs = _drop_settled_command_output(state.runs)

    snapshot = None
    if read is not None and not read.has_partial_tail:
        snapshot = EvidenceSnapshot(
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
            claims=tuple(state.claims),
            degraded=tuple(state.degraded),
            parsed_from_offset=resume.watermark if resume is not None else 0,
            latest_record_at=state.latest_record_at,
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

    return (
        TranscriptEvidence(
            validation_runs=tuple(run for run in state.runs if run.categories),
            command_runs=tuple(run for run in state.runs if not run.categories),
            edits=tuple(state.edits),
            task_claims=tuple(state.claims),
            attempted_paths=tuple(attempted_paths),
            sessions=(session.id,),
            degraded_capabilities=tuple(dict.fromkeys(state.degraded)),
            latest_record_at=state.latest_record_at,
        ),
        snapshot,
    )


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
) -> tuple[list[str], list[str]]:
    primary, attempted = _resolve_transcript_path(session, archive_dir, local_machine_id)
    if primary is None:
        return [], attempted
    supplemental = find_supplemental_transcripts_on_disk(session.source, primary)
    return [primary, *supplemental], [*attempted, *supplemental]


def _consume_message(state: _DerivationState, message: ParsedMessage) -> None:
    timestamp = _as_utc(message.timestamp)
    if not _inside_window(timestamp, state.window_start):
        return
    order = state.next_order()
    call_id = message.tool_use_id
    if state.session.source == "claude":
        recover_background_receipt(state.runs, message, state.pending.get(call_id or ""), order)
    if message.content_type == "tool_use":
        name = message.tool_name or ""
        arguments = message.tool_input or {}
        if call_id:
            state.pending[call_id] = PendingTool(
                name, arguments, timestamp, order, call_id, workdir=tool_workdir(message.raw_json)
            )
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
            state.pending[call_id] = PendingTool(
                name, event.arguments, timestamp, order, call_id, event.server
            )
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
    if not outcome.command.strip():
        return
    if _is_unexecuted_tool_result(outcome.result):
        return
    match = matches[0] if matches else None
    segments = _validation_segments(matches)
    output, output_truncated = _extract_output(outcome.result)
    status, exit_code, unknown_reason = _extract_outcome(
        outcome.result,
        output,
        aggregate_status_is_trustworthy=(
            not match.is_compound
            if match
            else not classify_validation_command_equivalence(outcome.command).wrapped
        ),
    )
    output, output_truncated = _retained_output(outcome.command, segments, output, output_truncated)
    provenance = outcome.result.get("outcome_provenance")
    if provenance == "codex.functions_exec.wrapper" and state.runs:
        prior = state.runs[-1]
        elapsed = (completed_at - prior.completed_at).total_seconds()
        if (
            prior.source == "codex"
            and prior.exit_code is not None
            and (
                segments
                or (
                    prior.core_command is not None
                    and prior.core_command
                    == classify_validation_command_equivalence(outcome.command).core_command
                )
            )
            and prior.validation_segments == segments
            and prior.output == output
            and 0 <= elapsed <= 1
        ):
            return
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
            f"codex could not recover a definitive outcome for {match.label if match else 'command'}: "
            f"{unknown_reason or 'unknown result'}"
        )
    state.runs.append(
        TranscriptValidationRun(
            session_id=state.session.id,
            source=state.session.source,
            command=outcome.command,
            categories=_segment_categories(segments),
            matcher_id=match.matcher_id if match else "shell-command",
            label=match.label if match else "Shell command",
            outcome=status,
            started_at=completed_at,
            completed_at=completed_at,
            order=order,
            exit_code=exit_code,
            unknown_reason=unknown_reason,
            output=output,
            output_truncated=output_truncated,
            workdir=outcome.workdir,
            validation_segments=segments,
        )
    )
    _recover_rtk_output(state, outcome.result)


def _validation_segments(
    matches: Sequence[ValidationCommandMatch],
) -> tuple[TranscriptValidationSegment, ...]:
    """One record per distinct validation segment, in command order."""
    return tuple(
        dict.fromkeys(
            TranscriptValidationSegment(
                command=match.normalized_command,
                categories=match.categories,
                segment_index=match.segment_index,
                languages=match.languages,
                bounded_inputs=match.bounded_inputs,
            )
            for match in matches
        )
    )


def _segment_categories(segments: Sequence[TranscriptValidationSegment]) -> tuple[str, ...]:
    """Union of the segments' categories, first occurrence first."""
    return tuple(dict.fromkeys(category for segment in segments for category in segment.categories))


def _record_validation_run(
    state: _DerivationState,
    pending: PendingTool,
    *,
    result: Any,
    completed_at: datetime,
    order: int,
    source_label: str,
) -> None:
    if _is_unexecuted_tool_result(result, source=state.session.source):
        # Begin records are provisional until an explicit denial proves no edit ran.
        state.edits[:] = [edit for edit in state.edits if edit.order != pending.order]
        return
    if _tool_basename(pending.name) not in _SHELL_TOOLS:
        if claim := task_claim(pending, result, completed_at):
            state.claims.append(claim)
        confirmed = _extract_outcome(result)[0] == "success"
        native_result = result.get("tool_result", result) if isinstance(result, dict) else result
        output = native_result.get("content") if isinstance(native_result, dict) else native_result
        written_path = pending.arguments.get("file_path")
        creation_receipt = (
            confirmed
            and _tool_basename(pending.name) == "write"
            and isinstance(written_path, str)
            and isinstance(output, str)
            and (
                output == f"File created successfully at: {written_path}"
                or output.startswith(f"File created successfully at: {written_path} ")
            )
        )
        state.edits[:] = [
            replace(
                edit,
                source_confirmed=confirmed,
                source_confirmed_at=completed_at if confirmed else None,
                source_created=bool(
                    creation_receipt
                    and not any(
                        prior.path == edit.path and prior.order < edit.order
                        for prior in state.edits
                    )
                ),
            )
            if edit.order == pending.order
            else edit
            for edit in state.edits
        ]
        return
    command = _extract_command(pending.arguments)
    matches = classify_validation_segments(command, state.detection_config)
    if not command.strip():
        return
    match = matches[0] if matches else None
    segments = _validation_segments(matches)
    # A literal recall carries the original failure sections, often larger than
    # the ordinary command summary. Keep that native receipt bounded separately.
    recall = _RTK_RECALL_RE.fullmatch(command.strip())
    output, output_truncated = (
        _extract_output(result, max_chars=64_000) if recall else _extract_output(result)
    )
    outcome, exit_code, unknown_reason = _extract_outcome(
        result,
        output,
        aggregate_status_is_trustworthy=(
            not match.is_compound
            if match
            else not classify_validation_command_equivalence(command).wrapped
        ),
    )
    if outcome == "unknown":
        state.degraded.append(
            f"{source_label} lacks a definitive exit outcome for {match.label if match else 'command'}; "
            "re-run the command in a supported shell tool"
        )
    output, output_truncated = _retained_output(command, segments, output, output_truncated)
    state.runs.append(
        TranscriptValidationRun(
            session_id=state.session.id,
            source=state.session.source,
            command=command,
            categories=_segment_categories(segments),
            matcher_id=match.matcher_id if match else "shell-command",
            label=match.label if match else "Shell command",
            outcome=outcome,
            started_at=pending.timestamp,
            completed_at=completed_at,
            order=order,
            exit_code=exit_code,
            unknown_reason=unknown_reason,
            output=output,
            output_truncated=output_truncated,
            workdir=tool_workdir(pending.arguments) or pending.workdir,
            validation_segments=segments,
        )
    )
    if state.session.source == "claude":
        state.runs[-1] = pending_background_run(state.runs[-1], pending.call_id)
    _recover_rtk_output(state, result)


def _recover_rtk_output(state: _DerivationState, result: Any) -> None:
    """Attach a native recall receipt to its unique original failed test run."""
    receipt = state.runs[-1]
    match = _RTK_RECALL_RE.fullmatch(receipt.command.strip())
    if match is None or receipt.wrapped:
        return
    # Retrieval output contains the old pytest failure. Its transport outcome,
    # rather than those historical failure counts, certifies the recall itself.
    outcome, exit_code, unknown_reason = _extract_outcome(result)
    if outcome != "success" or not receipt.output or receipt.output_truncated:
        return
    state.runs[-1] = replace(
        receipt, outcome=outcome, exit_code=exit_code, unknown_reason=unknown_reason
    )
    reference = re.compile(rf"(?m)^[ \t]*\[full output: rtk recall {match.group(1)}\][ \t]*\r?$")
    originals = [
        index
        for index, run in enumerate(state.runs[:-1])
        if run.outcome == "failure"
        and "test" in run.categories
        and (
            reference.search(run.output or "")
            or re.fullmatch(
                rf"(?:uv run )?rtk recall {match.group(1)}", run.output_recovered_from or ""
            )
        )
    ]
    if len(originals) != 1:
        return
    index = originals[0]
    state.runs[index] = replace(
        state.runs[index],
        output=receipt.output,
        output_truncated=False,
        output_recovered_from=receipt.command,
        output_recovered_at=receipt.completed_at,
    )


def _shell_write_paths(
    command: str, arguments: dict[str, Any], repo_path: str, require_proven_checkout: bool
) -> set[str]:
    """Credit writes recognized by the canonical TDD shell classifier."""
    if not command.strip():
        return set()
    write_paths = _shell_tool_metadata(command).get("canonical_write_file_paths")
    if not isinstance(write_paths, list):
        return set()
    return {
        resolved
        for path in write_paths
        if isinstance(path, str) and path
        if (resolved := _resolve_edit_path(path, arguments, repo_path, require_proven_checkout))
        is not None
    }


def _record_edit(
    state: _DerivationState,
    tool_name: str,
    arguments: dict[str, Any],
    timestamp: datetime,
    order: int,
) -> None:
    basename = _tool_basename(tool_name)
    require_proven_checkout = state.task_checkout_paths is not None
    if basename in _EDIT_TOOLS:
        paths = _extract_edit_paths(
            basename, arguments, state.repo_path, require_proven_checkout=require_proven_checkout
        )
    elif basename in _SHELL_TOOLS:
        paths = _shell_write_paths(
            _extract_command(arguments), arguments, state.repo_path, require_proven_checkout
        )
    else:
        return
    for path in paths:
        task_file = _match_task_file(
            path, state.task_edited_files, state.repo_path, state.task_checkout_paths
        )
        if task_file is None:
            continue
        previous = next((edit for edit in reversed(state.edits) if edit.path == task_file), None)
        source_after = (
            edited_source(
                basename,
                arguments,
                previous.source_after
                if previous is not None and previous.source_confirmed
                else None,
            )
            if task_file.endswith(".py")
            else None
        )
        old, new = arguments.get("old_string"), arguments.get("new_string")
        python_edit = basename == "edit" and task_file.endswith(".py")
        fragment: str | None = None
        stub: tuple[str, tuple[str, ...]] | None = None
        added: str | None = None
        unchanged = False
        if python_edit and isinstance(old, str) and isinstance(new, str):
            fragment = new
            stub = python_keyword_stub(old, new)
            added = python_added_source(old, new)
            old_tokens = python_edit_tokens(old)
            unchanged = bool(old_tokens and old_tokens == python_edit_tokens(new))
        state.edits.append(
            TranscriptEdit(
                session_id=state.session.id,
                source=state.session.source,
                path=task_file,
                timestamp=timestamp,
                order=order,
                tool_name=tool_name,
                source_after=source_after,
                source_fragment=fragment,
                python_stub=stub,
                python_added_source=added,
                source_unchanged=unchanged,
            )
        )


def _observe_record_time(state: _DerivationState, timestamp: datetime) -> None:
    """Track the newest parsed record so close can recognize a lagging transcript."""
    stamp = _as_utc(timestamp)
    if state.latest_record_at is None or stamp > state.latest_record_at:
        state.latest_record_at = stamp


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
    "derive_transcript_evidence",
    "merge_transcript_evidence",
]
