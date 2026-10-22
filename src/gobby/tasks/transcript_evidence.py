"""Derive validation outcomes directly from provider transcripts."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast

from gobby.config.validation_detection import (
    ValidationCommandMatch,
    ValidationDetectionConfig,
    classify_validation_segments,
)
from gobby.sessions.machine_scope import require_local_session_ownership
from gobby.sessions.transcript_io import _iter_archive_lines
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import (
    ParsedMessage,
    ParsedToolEvent,
)
from gobby.storage.session_models import Session
from gobby.tasks.transcript_background import (
    pending_background_run,
    recover_background_receipt,
)
from gobby.tasks.transcript_evidence_merge import archive_before_live, merge_transcript_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
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
from gobby.tasks.transcript_evidence_snapshots import (
    derivation_fingerprint as _derivation_fingerprint,
)
from gobby.tasks.transcript_evidence_sources import (
    _as_utc,
    _resolve_transcript_paths,
    select_window_raw_lines,
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
from gobby.tasks.transcript_output_retention import (
    recover_rtk_output as _recover_rtk_output,
)
from gobby.tasks.transcript_tool_arguments import (
    extract_command as _extract_command,
)
from gobby.tasks.transcript_tool_arguments import (
    normalize_tool_name as _tool_basename,
)
from gobby.tasks.transcript_tool_arguments import (
    tool_workdir,
)

logger = logging.getLogger(__name__)

# How much transcript before the claim window still reaches the parser.
# Lines older than the claim window only cost parsing. On an 83 MB / 53k-line
# session, filtering cut derivation from 420 ms to 219 ms (#20866).
#
# Narrowing is exact for the records themselves — a differential run over that
# transcript produced identical evidence for 30-minute, 7-hour, and full-session
# windows. The lookback preserves cross-line parser state for tool calls that
# straddle the boundary; absent or non-UTC timestamps are always kept.


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


@dataclass
class _DerivationState:
    session: Session
    detection_config: ValidationDetectionConfig
    window_start: datetime | None
    pending: dict[str, PendingTool] = field(default_factory=dict)
    runs: list[TranscriptValidationRun] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    order: int = 0
    latest_record_at: datetime | None = None

    def next_order(self) -> int:
        self.order += 1
        return self.order


async def derive_transcript_evidence(
    session: Session,
    window_start: datetime | str | None,
    detection_config: ValidationDetectionConfig,
    *,
    archive_dir: str | None = None,
) -> TranscriptEvidence:
    """Parse provider transcripts for validation failures and their later resolutions."""
    local_machine_id = require_local_session_ownership(session)
    payload = await run_in_transcript_evidence_pool(
        _derive_chunked_transcript_evidence,
        session,
        _coerce_datetime(window_start),
        detection_config,
        archive_dir,
        local_machine_id,
    )
    return cast(TranscriptEvidence, await decode_cooperatively(payload))


def _derive_chunked_transcript_evidence(
    session: Session,
    window_start: datetime | None,
    detection_config: ValidationDetectionConfig,
    archive_dir: str | None,
    local_machine_id: str,
) -> ChunkedPayload:
    """Pool entry: records cross the boundary in chunks the event loop decodes."""
    return encode(
        _derive_transcript_evidence_sync(
            session,
            window_start,
            detection_config,
            archive_dir,
            local_machine_id,
            None,
        )[0]
    )


def _derive_transcript_evidence_sync(
    session: Session,
    window_start: datetime | None,
    detection_config: ValidationDetectionConfig,
    archive_dir: str | None,
    local_machine_id: str,
    resume: EvidenceSnapshot | None,
) -> tuple[TranscriptEvidence, EvidenceSnapshot | None]:
    fingerprint = _derivation_fingerprint(
        session,
        window_start,
        detection_config,
    )
    snapshot_key = f"{session.id}:{fingerprint}"
    if resume is None:
        resume = load_durable_snapshot(snapshot_key)
    paths, attempted_paths = _resolve_transcript_paths(
        session, archive_dir, local_machine_id, window_start
    )
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
    return merge_transcript_evidence(
        results[0][0],
        *(
            archive_before_live(evidence, results[0][0]) if path.endswith(".gz") else evidence
            for path, (evidence, _) in zip(paths[1:], results[1:], strict=True)
        ),
    ), updated


def _derive_transcript_path_evidence(
    session: Session,
    path: str,
    window_start: datetime | None,
    detection_config: ValidationDetectionConfig,
    attempted_paths: list[str],
    *,
    resume: EvidenceSnapshot | None,
) -> tuple[TranscriptEvidence, EvidenceSnapshot | None]:
    fingerprint = _derivation_fingerprint(
        session,
        window_start,
        detection_config,
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
        window_start=window_start,
    )
    if resume is not None:
        # Deep-copy on load: hydrated parsers and the pending map may share
        # containers with the live parse, and the cached snapshot must stay
        # exactly what the previous derivation stored.
        parser.hydrate_state(deepcopy(resume.parser_state))
        state.pending = deepcopy(resume.pending)
        state.runs = list(resume.runs)
        state.degraded = list(resume.degraded)
        state.order = resume.order
        state.latest_record_at = resume.latest_record_at

    for event in parser.iter_parse_events(select_window_raw_lines(lines, window_start)):
        for outcome in event.codex_exec_outcomes:
            _consume_codex_outcome(state, outcome)
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
            degraded=tuple(state.degraded),
            parsed_from_offset=resume.watermark if resume is not None else 0,
            latest_record_at=state.latest_record_at,
        )
    logger.debug(
        "Derived validation transcript evidence",
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
            attempted_paths=tuple(attempted_paths),
            sessions=(session.id,),
            degraded_capabilities=tuple(dict.fromkeys(state.degraded)),
            latest_record_at=state.latest_record_at,
        ),
        snapshot,
    )


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
    recall = _RTK_RECALL_RE.fullmatch(outcome.command.strip())
    output, output_truncated = (
        _extract_output(outcome.result, max_chars=64_000)
        if recall
        else _extract_output(outcome.result)
    )
    provenance = outcome.result.get("outcome_provenance")
    outcome_fields = outcome.result
    if provenance == "codex.functions_exec.wrapper":
        # The wrapper's output is whatever the cell printed, which can be command
        # stdout spelling an exit code, so only the wrapper's own fields count (#23724).
        outcome_fields = {key: value for key, value in outcome.result.items() if key != "output"}
    status, exit_code, unknown_reason = _extract_outcome(
        outcome_fields,
        output,
        aggregate_status_is_trustworthy=(
            not match.is_compound
            if match
            else not classify_validation_command_equivalence(outcome.command).wrapped
        ),
    )
    output, output_truncated = _retained_output(outcome.command, segments, output, output_truncated)
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
    _recover_rtk_output(state.runs, outcome.result)


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
        return
    if _tool_basename(pending.name) not in _SHELL_TOOLS:
        return
    command = _extract_command(pending.arguments)
    matches = classify_validation_segments(command, state.detection_config)
    if not command.strip():
        return
    match = matches[0] if matches else None
    segments = _validation_segments(matches)
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
        description = pending.arguments.get("description")
        state.runs[-1] = pending_background_run(
            state.runs[-1],
            pending.call_id,
            description=description
            if isinstance(description, str) and description.strip()
            else None,
        )
    _recover_rtk_output(state.runs, result)


def _observe_record_time(state: _DerivationState, timestamp: datetime) -> None:
    """Track the newest record represented in the snapshot."""
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


__all__ = [
    "derive_transcript_evidence",
]
