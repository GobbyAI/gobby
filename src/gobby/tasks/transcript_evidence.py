"""Derive task-close validation evidence directly from provider transcripts."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from gobby.config.validation_detection import (
    ValidationDetectionConfig,
    classify_validation_command,
)
from gobby.sessions.machine_scope import require_local_session_ownership
from gobby.sessions.transcript_archive import get_archive_dir
from gobby.sessions.transcript_io import _iter_archive_lines, _iter_jsonl_lines
from gobby.sessions.transcript_paths import find_transcript_on_disk
from gobby.sessions.transcript_tool_metadata import extract_result_metadata
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import (
    ParsedMessage,
    ParsedToolEvent,
    raw_lines_from_texts,
)
from gobby.storage.session_models import Session

EvidenceOutcome = Literal["success", "failure", "unknown"]

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
_EDIT_TOOLS = {"edit", "multiedit", "notebookedit", "write", "apply_patch"}
_COMMAND_KEYS = ("cmd", "command", "script")
_PATH_KEYS = ("file_path", "path", "notebook_path")
_EXIT_CODE_KEYS = ("exit_code", "exitCode")
_SUCCESS_STATUSES = {"completed", "ok", "passed", "success", "succeeded"}
_FAILURE_STATUSES = {"error", "failed", "failure"}
_OUTPUT_CHAR_LIMIT = 16_000


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

    try:
        lines = list(_iter_archive_lines(path) if path.endswith(".gz") else _iter_jsonl_lines(path))
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
        task_edited_files={_normalize_known_path(path, repo_path) for path in task_edited_files},
        repo_path=repo_path,
        window_start=window_start,
    )
    for event in parser.iter_parse_events(raw_lines_from_texts(lines)):
        for outcome in event.codex_exec_outcomes:
            _consume_codex_outcome(state, outcome)
        for record in event.records:
            if isinstance(record, ParsedMessage):
                _consume_message(state, record)
            elif isinstance(record, ParsedToolEvent):
                _consume_tool_event(state, record)

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
    match = classify_validation_command(outcome.command, state.detection_config)
    if match is None:
        return
    status, exit_code, unknown_reason = _extract_outcome(outcome.result)
    output, output_truncated = _extract_output(outcome.result)
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
    match = classify_validation_command(command, state.detection_config)
    if match is None:
        return
    outcome, exit_code, unknown_reason = _extract_outcome(result)
    output, output_truncated = _extract_output(result)
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
    if tool_name == "apply_patch":
        raw = arguments.get("raw") or arguments.get("patch") or arguments.get("input")
        if isinstance(raw, str):
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


def _extract_outcome(result: Any) -> tuple[EvidenceOutcome, int | None, str | None]:
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
    "derive_transcript_evidence",
    "merge_transcript_evidence",
]
