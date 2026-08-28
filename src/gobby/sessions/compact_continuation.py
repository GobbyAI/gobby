"""Continuation scheduling for Gobby-initiated terminal compactions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from gobby.sessions.compact_markers import (
    COMPACT_HANDOFF_MARKER_VARIABLE,
    COMPACT_RESUME_ADVISORY_SKILL_VARIABLE_KEYS,
    COMPACT_RESUME_ADVISORY_SKILLS_VARIABLE,
    COMPACT_RESUME_EXCLUDED_SKILLS,
    COMPACT_RESUME_REQUIRED_SKILL_VARIABLE_KEYS,
    COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE,
    HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS,
    HANDOFF_COMPACT_CONTINUE_SEND_DELAY_SECONDS,
    HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
    LOADING_SKILLS_NAME,
    WORKFLOW_REQUESTED_SKILLS_VARIABLE,
)
from gobby.sessions.handoff import build_handoff_continue_prompt
from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.sessions.tmux_context import parse_terminal_context_value
from gobby.storage.hub.protocol import SessionVariableMutation
from gobby.storage.session_models import Session
from gobby.terminals.lookup import manager_for_terminal_context

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

__all__ = [
    "COMPACT_HANDOFF_MARKER_VARIABLE",
    "COMPACT_RESUME_ADVISORY_SKILLS_VARIABLE",
    "COMPACT_RESUME_ADVISORY_SKILL_VARIABLE_KEYS",
    "COMPACT_RESUME_EXCLUDED_SKILLS",
    "COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE",
    "COMPACT_RESUME_REQUIRED_SKILL_VARIABLE_KEYS",
    "HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS",
    "HANDOFF_COMPACT_CONTINUE_SEND_DELAY_SECONDS",
    "HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS",
    "HANDOFF_COMPACT_CONTINUE_VARIABLE",
    "CodexRolloutCursor",
    "CodexRolloutObservationError",
    "LOADING_SKILLS_NAME",
    "WORKFLOW_REQUESTED_SKILLS_VARIABLE",
]

logger = logging.getLogger(__name__)

_HANDOFF_COMPACT_CONTINUATION_TASKS: set[asyncio.Task[Any]] = set()

_CODEX_COMPACT_READY_STATUS_LINE = "• Context compacted"
_CODEX_COMPACT_READY_POLL_SECONDS = 0.25
CODEX_COMPACT_READY_CAPTURE_LINES = 100


class CodexRolloutObservationError(RuntimeError):
    """Raised when a rollout can no longer be observed safely."""


@dataclass
class CodexRolloutCursor:
    """Read only fresh, complete JSONL records from a stable rollout file."""

    path: Path
    device: int
    inode: int
    offset: int
    _buffer: bytes = field(default=b"", repr=False)
    _discard_historical_partial: bool = field(default=False, repr=False)

    @classmethod
    def at_eof(cls, transcript_path: str | Path | None) -> CodexRolloutCursor:
        """Create a cursor at the current EOF of a readable rollout."""
        if transcript_path is None or not str(transcript_path).strip():
            raise CodexRolloutObservationError("Codex session has no rollout transcript path")

        path = Path(transcript_path).expanduser()
        try:
            with path.open("rb") as stream:
                file_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    raise CodexRolloutObservationError(
                        f"Codex rollout transcript is not a regular file: {path}"
                    )
                discard_historical_partial = False
                if file_stat.st_size:
                    stream.seek(-1, os.SEEK_END)
                    discard_historical_partial = stream.read(1) != b"\n"
        except CodexRolloutObservationError:
            raise
        except OSError as exc:
            raise CodexRolloutObservationError(
                f"Codex rollout transcript is unavailable: {path}: {exc}"
            ) from exc

        return cls(
            path=path,
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            offset=file_stat.st_size,
            _discard_historical_partial=discard_historical_partial,
        )

    def saw_fresh_turn_aborted(self) -> bool:
        """Return whether newly appended complete records contain turn_aborted."""
        chunk = self._read_appended_bytes()
        if not chunk:
            return False

        if self._discard_historical_partial:
            newline = chunk.find(b"\n")
            if newline < 0:
                return False
            chunk = chunk[newline + 1 :]
            self._discard_historical_partial = False

        records = (self._buffer + chunk).split(b"\n")
        self._buffer = records.pop()
        for raw_record in records:
            try:
                record = json.loads(raw_record.rstrip(b"\r"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict) or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "turn_aborted":
                return True
        return False

    def _read_appended_bytes(self) -> bytes:
        try:
            path_stat = self.path.stat()
            self._validate_stat(path_stat)
            with self.path.open("rb") as stream:
                opened_stat = os.fstat(stream.fileno())
                self._validate_stat(opened_stat)
                stream.seek(self.offset)
                chunk = stream.read()
                read_stat = os.fstat(stream.fileno())
            final_path_stat = self.path.stat()
            self._validate_stat(read_stat, minimum_size=self.offset + len(chunk))
            self._validate_stat(final_path_stat, minimum_size=self.offset + len(chunk))
        except CodexRolloutObservationError:
            raise
        except OSError as exc:
            raise CodexRolloutObservationError(
                f"Codex rollout transcript became unavailable: {self.path}: {exc}"
            ) from exc

        self.offset += len(chunk)
        return chunk

    def _validate_stat(self, file_stat: os.stat_result, *, minimum_size: int | None = None) -> None:
        if file_stat.st_dev != self.device or file_stat.st_ino != self.inode:
            raise CodexRolloutObservationError(
                f"Codex rollout transcript was replaced: {self.path}"
            )
        required_size = self.offset if minimum_size is None else minimum_size
        if file_stat.st_size < required_size:
            raise CodexRolloutObservationError(
                f"Codex rollout transcript was truncated: {self.path}"
            )


class CompactResumeSkillTiers(TypedDict):
    """Skill names captured before compact resets the current-context ledgers."""

    required: list[str]
    advisory: list[str]


def mark_handoff_compact_continuation_pending(
    db: HubDatabase,
    session_id: str,
    *,
    prompt: str | None = None,
    attempt_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Store the pending continuation marker on the compacting session."""
    payload = {
        "prompt": prompt or build_handoff_continue_prompt(),
        "created_at": _format_timestamp(now or datetime.now(UTC)),
    }
    if attempt_id:
        payload["attempt_id"] = attempt_id
    try:
        _merge_session_variable(db, session_id, HANDOFF_COMPACT_CONTINUE_VARIABLE, payload)
        return True
    except Exception:
        logger.warning(
            "Failed to mark set_handoff compact continuation pending for session %s",
            session_id,
            exc_info=True,
        )
        return False


def persist_handoff_resume_skills(
    db: HubDatabase,
    session_id: str,
) -> CompactResumeSkillTiers:
    """Persist both pre-compact resume tiers and return them."""
    variables = _load_session_variables(db, session_id)
    skill_tiers = _collect_compact_resume_required_skills(variables)
    for variable, values in (
        (COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE, skill_tiers["required"]),
        (COMPACT_RESUME_ADVISORY_SKILLS_VARIABLE, skill_tiers["advisory"]),
    ):
        try:
            _merge_session_variable(db, session_id, variable, values)
        except Exception:
            logger.warning(
                "Failed to persist compact resume skills variable %s for session %s",
                variable,
                session_id,
                exc_info=True,
            )
    return skill_tiers


def consume_compact_handoff_marker(db: HubDatabase, session_id: str) -> bool:
    """Consume the one-shot compact marker after successful in-place reactivation.

    The marker (session variable ``handoff_source``) is written by the
    pre-compact rule and read by session-start classification; consuming it
    here keeps ordinary later restarts from classifying as compact and lets
    the stale-compact retention sweep skip resumed sessions.
    """
    return _pop_session_variable(db, session_id, COMPACT_HANDOFF_MARKER_VARIABLE) is not None


def clear_handoff_compact_continuation_pending(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str | None = None,
) -> bool:
    """Clear the pending continuation marker if it exists."""
    try:
        removed = _pop_session_variable(
            db,
            session_id,
            HANDOFF_COMPACT_CONTINUE_VARIABLE,
            expected_attempt_id=attempt_id,
        )
        return attempt_id is None or removed is not None
    except Exception:
        logger.warning(
            "Failed to clear set_handoff compact continuation pending for session %s",
            session_id,
            exc_info=True,
        )
        return False


def consume_handoff_compact_continuation_pending(
    db: HubDatabase,
    session_id: str,
    *,
    now: datetime | None = None,
    fresh_seconds: int = HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS,
) -> str | None:
    """Consume a fresh pending marker and return its prompt."""
    pending = _take_handoff_compact_continuation_pending(
        db,
        session_id,
        now=now,
        fresh_seconds=fresh_seconds,
    )
    return pending[0] if pending is not None else None


def _take_handoff_compact_continuation_pending(
    db: HubDatabase,
    session_id: str,
    *,
    now: datetime | None = None,
    fresh_seconds: int = HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS,
    expected_attempt_id: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Atomically take a fresh marker while retaining its exact payload."""
    try:
        value = _pop_session_variable(
            db,
            session_id,
            HANDOFF_COMPACT_CONTINUE_VARIABLE,
            expected_attempt_id=expected_attempt_id,
        )
    except Exception:
        logger.warning(
            "Failed to consume set_handoff compact continuation pending for session %s",
            session_id,
            exc_info=True,
        )
        return None

    if not isinstance(value, dict):
        return None

    created_at = _parse_timestamp(value.get("created_at"))
    if created_at is None:
        return None

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    age_seconds = (current_time - created_at).total_seconds()
    if age_seconds < 0 or age_seconds > fresh_seconds:
        return None

    prompt = value.get("prompt")
    resolved_prompt = (
        prompt if isinstance(prompt, str) and prompt.strip() else build_handoff_continue_prompt()
    )
    return resolved_prompt, value


def schedule_handoff_compact_continuation(
    session: Any,
    prompt: str,
    *,
    loop: Any | None = None,
    delay_seconds: float = HANDOFF_COMPACT_CONTINUE_SEND_DELAY_SECONDS,
) -> bool:
    """Schedule a best-effort tmux prompt send without blocking SessionStart."""
    session_id = getattr(session, "id", "unknown")
    ctx = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if ctx is None:
        logger.debug("Cannot schedule set_handoff compact continuation; no terminal context")
        return False

    target = ctx.get("tmux_pane") or ctx.get("tmux_session")
    if not target:
        logger.debug(
            "Cannot schedule set_handoff compact continuation for session %s; no tmux target",
            session_id,
        )
        return False

    tmux = manager_for_terminal_context(ctx)
    coro = _send_handoff_compact_continuation(
        tmux,
        str(target),
        prompt,
        str(session_id),
        delay_seconds=delay_seconds,
    )
    return _schedule_coroutine(coro, loop=loop)


def schedule_codex_handoff_compact_continuation_readiness(
    db: HubDatabase,
    *,
    pending_session_id: str,
    target_session: Any,
    before_command: str | None,
    attempt_id: str | None = None,
    loop: Any | None = None,
    poll_seconds: float = _CODEX_COMPACT_READY_POLL_SECONDS,
) -> bool:
    """Wait for Codex's terminal completion signal before submitting continuation."""
    if before_command is None:
        logger.debug(
            "Cannot schedule Codex compact readiness for session %s; baseline capture failed",
            pending_session_id,
        )
        return False

    ctx = parse_terminal_context_value(getattr(target_session, "terminal_context", None))
    if ctx is None:
        logger.debug("Cannot schedule Codex compact readiness; no terminal context")
        return False

    target = ctx.get("tmux_pane") or ctx.get("tmux_session")
    if not target:
        logger.debug(
            "Cannot schedule Codex compact readiness for session %s; no tmux target",
            pending_session_id,
        )
        return False

    tmux = manager_for_terminal_context(ctx)
    coro = _continue_after_codex_compaction_ready(
        db,
        tmux=tmux,
        target=str(target),
        pending_session_id=pending_session_id,
        before_command=before_command,
        poll_seconds=poll_seconds,
        attempt_id=attempt_id,
    )
    return _schedule_coroutine(coro, loop=loop)


def consume_and_schedule_handoff_compact_continuation(
    db: HubDatabase,
    *,
    pending_session_id: str | None,
    target_session: Any,
    loop: Any | None = None,
) -> bool:
    """Consume a fresh marker and schedule its continuation prompt.

    Compaction is in-place on one pane. set_handoff may persist the marker on
    the MCP-resolved row while PostCompact arrives on the provider hook row;
    both identify the same terminal process, so a unique same-pane marker is
    still this compact's continuation.
    """
    if not pending_session_id:
        return False
    pending = _take_handoff_compact_continuation_pending(db, pending_session_id)
    source_session_id = pending_session_id
    if pending is None:
        sibling = _take_same_terminal_handoff_compact_continuation_pending(
            db,
            pending_session_id,
            target_session,
        )
        if sibling is None:
            return False
        source_session_id, pending = sibling
    prompt, payload = pending
    if schedule_handoff_compact_continuation(target_session, prompt, loop=loop):
        return True
    try:
        _restore_session_variable_if_absent(
            db,
            source_session_id,
            HANDOFF_COMPACT_CONTINUE_VARIABLE,
            payload,
        )
    except Exception:
        logger.warning(
            "Failed to restore set_handoff compact continuation pending for session %s",
            source_session_id,
            exc_info=True,
        )
    return False


def _take_same_terminal_handoff_compact_continuation_pending(
    db: HubDatabase,
    pending_session_id: str,
    target_session: Any,
) -> tuple[str, tuple[str, dict[str, Any]]] | None:
    """Take the unique fresh marker on the same live terminal process."""
    target_context = getattr(target_session, "terminal_context", None)
    if parse_terminal_context_value(target_context) is None:
        return None
    try:
        rows = db.fetchall(
            """
            SELECT s.*
              FROM sessions s
              JOIN session_variables sv ON sv.session_id = s.id
             WHERE s.id <> %s
               AND s.session_type = 'terminal'
               AND s.status <> 'deleted'
               AND jsonb_typeof(sv.variables -> %s) = 'object'
            """,
            (pending_session_id, HANDOFF_COMPACT_CONTINUE_VARIABLE),
        )
    except Exception:
        logger.warning(
            "Failed listing same-terminal set_handoff compact markers for session %s",
            pending_session_id,
            exc_info=True,
        )
        return None
    matching = [
        candidate
        for row in rows
        if terminal_process_contexts_match(
            (candidate := Session.from_row(row)).terminal_context,
            target_context,
        )
    ]
    if len(matching) != 1:
        return None
    taken = _take_handoff_compact_continuation_pending(db, matching[0].id)
    if taken is None:
        return None
    return matching[0].id, taken


async def _send_handoff_compact_continuation(
    tmux: Any,
    target: str,
    prompt: str,
    session_id: str,
    *,
    delay_seconds: float,
) -> bool:
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    try:
        ok = await tmux.dispatch_keys(target, f"{prompt}\n", literal=True)
    except Exception:
        logger.warning(
            "Failed to send set_handoff compact continuation prompt for session %s",
            session_id,
            exc_info=True,
        )
        return False
    if not ok:
        logger.warning(
            "tmux send-keys returned false for set_handoff compact continuation %s", session_id
        )
        return False
    # A composer still settling the bracketed paste can swallow the Enter that
    # send_keys appended; this second Enter submits in that case and is a no-op
    # on an already-submitted (empty) composer. Delivery already succeeded, so
    # a retry failure is logged, never propagated.
    await asyncio.sleep(HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS)
    try:
        retry_ok = await tmux.dispatch_keys(target, "Enter", literal=False)
    except Exception:
        retry_ok = False
        logger.warning(
            "Failed follow-up Enter for set_handoff compact continuation %s",
            session_id,
            exc_info=True,
        )
    if not retry_ok:
        logger.warning(
            "tmux follow-up Enter returned false for set_handoff compact continuation %s",
            session_id,
        )
    return True


async def _continue_after_codex_compaction_ready(
    db: HubDatabase,
    *,
    tmux: Any,
    target: str,
    pending_session_id: str,
    before_command: str,
    poll_seconds: float,
    attempt_id: str | None = None,
    fresh_seconds: int = HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS,
) -> None:
    """Consume and submit only after Codex renders a fresh completion marker."""
    baseline_count = _count_codex_compact_ready_status_lines(before_command)
    deadline = asyncio.get_running_loop().time() + fresh_seconds

    while asyncio.get_running_loop().time() <= deadline:
        try:
            variables = await asyncio.to_thread(
                _load_session_variables,
                db,
                pending_session_id,
            )
        except Exception:
            logger.warning(
                "Failed to load Codex compact continuation marker for session %s",
                pending_session_id,
                exc_info=True,
            )
            return
        pending_payload = variables.get(HANDOFF_COMPACT_CONTINUE_VARIABLE)
        if pending_payload is None:
            return
        if attempt_id is not None and (
            not isinstance(pending_payload, dict) or pending_payload.get("attempt_id") != attempt_id
        ):
            logger.debug(
                "Stopped stale Codex compact readiness watcher for session %s",
                pending_session_id,
            )
            return

        try:
            output = await tmux.snapshot_lines(
                target,
                lines=CODEX_COMPACT_READY_CAPTURE_LINES,
            )
        except Exception:
            logger.debug(
                "Failed to inspect Codex compact readiness for session %s",
                pending_session_id,
                exc_info=True,
            )
            return
        if not isinstance(output, str):
            logger.debug(
                "Stopped Codex compact readiness watcher after pane disappeared for session %s",
                pending_session_id,
            )
            return

        fresh_output = _fresh_terminal_output(before_command, output)
        ready = (
            _count_codex_compact_ready_status_lines(output) > baseline_count
            or _count_codex_compact_ready_status_lines(fresh_output) > 0
        )
        if ready:
            if poll_seconds > 0:
                await asyncio.sleep(poll_seconds)
            pending = await asyncio.to_thread(
                _take_handoff_compact_continuation_pending,
                db,
                pending_session_id,
                expected_attempt_id=attempt_id,
            )
            if pending is None:
                return
            prompt, payload = pending
            sent = await _send_handoff_compact_continuation(
                tmux,
                target,
                prompt,
                pending_session_id,
                delay_seconds=0,
            )
            if not sent:
                await asyncio.to_thread(
                    _restore_session_variable_if_absent,
                    db,
                    pending_session_id,
                    HANDOFF_COMPACT_CONTINUE_VARIABLE,
                    payload,
                )
            return

        if poll_seconds > 0:
            await asyncio.sleep(poll_seconds)

    logger.warning(
        "Timed out waiting for Codex compact readiness for session %s",
        pending_session_id,
    )


def _count_codex_compact_ready_status_lines(output: str) -> int:
    """Count complete Codex compaction status lines."""
    return sum(line.strip() == _CODEX_COMPACT_READY_STATUS_LINE for line in output.splitlines())


def _fresh_terminal_output(before: str, after: str) -> str:
    """Return output appended after a pane snapshot, including a rolling window."""
    if not before:
        return after
    if after.startswith(before):
        return after[len(before) :]

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    max_overlap = min(len(before_lines), len(after_lines))
    for overlap in range(max_overlap, 0, -1):
        if before_lines[-overlap:] == after_lines[:overlap]:
            return "\n".join(after_lines[overlap:])
    return ""


def _schedule_coroutine(coro: Any, *, loop: Any | None = None) -> bool:
    try:
        running_loop = asyncio.get_running_loop()
        # Fire-and-forget: the coroutine logs its own failures and must not block startup.
        task = running_loop.create_task(coro)
        _HANDOFF_COMPACT_CONTINUATION_TASKS.add(task)
        task.add_done_callback(_HANDOFF_COMPACT_CONTINUATION_TASKS.discard)
        return True
    except RuntimeError:
        pass

    if loop is not None:
        try:
            loop_is_usable = not loop.is_closed()
        except Exception:
            loop_is_usable = False

        if loop_is_usable:
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
                return True
            except Exception:
                logger.debug(
                    "Failed to schedule set_handoff compact continuation on loop", exc_info=True
                )
                # Scheduling failed before ownership transferred to an event loop.
                coro.close()
                return False

    thread = threading.Thread(
        target=_run_coroutine_thread,
        args=(coro,),
        name="gobby-compact-continuation",
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        logger.debug("Failed to start set_handoff compact continuation thread", exc_info=True)
        # The fallback thread never took ownership, so close the coroutine explicitly.
        coro.close()
        return False
    return True


def _run_coroutine_thread(coro: Any) -> None:
    try:
        asyncio.run(coro)
    except Exception:
        logger.debug("Failed to run set_handoff compact continuation task", exc_info=True)


def _merge_session_variable(
    db: HubDatabase,
    session_id: str,
    name: str,
    value: Any,
) -> None:
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        variables = _load_variables(_row_variables(row))
        variables[name] = value
        if row:
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
                (json.dumps(variables), now, session_id),
            )
        else:
            conn.execute(
                "INSERT INTO session_variables (session_id, variables, updated_at) "
                "VALUES (%s, %s, %s)",
                (session_id, json.dumps(variables), now),
            )


def _restore_session_variable_if_absent(
    db: HubDatabase,
    session_id: str,
    name: str,
    value: Any,
) -> bool:
    """Restore a consumed value without replacing a concurrently written value."""
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        variables = _load_variables(_row_variables(row))
        if name in variables:
            return False
        variables[name] = value
        if row:
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
                (json.dumps(variables), now, session_id),
            )
        else:
            conn.execute(
                "INSERT INTO session_variables (session_id, variables, updated_at) "
                "VALUES (%s, %s, %s)",
                (session_id, json.dumps(variables), now),
            )
        return True


def _load_session_variables(db: HubDatabase, session_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    return _load_variables(_row_variables(row))


def _collect_compact_resume_required_skills(
    variables: dict[str, Any],
) -> CompactResumeSkillTiers:
    required: list[str] = []
    for key in COMPACT_RESUME_REQUIRED_SKILL_VARIABLE_KEYS:
        _extend_unique_strings(required, variables.get(key))

    advisory: list[str] = []
    for key in COMPACT_RESUME_ADVISORY_SKILL_VARIABLE_KEYS:
        _extend_unique_strings(advisory, variables.get(key))

    return _prepare_compact_resume_skill_tiers({"required": required, "advisory": advisory})


def _prepare_compact_resume_skill_tiers(
    skill_tiers: CompactResumeSkillTiers,
) -> CompactResumeSkillTiers:
    required = [
        skill
        for skill in _unique_strings(skill_tiers["required"])
        if skill not in COMPACT_RESUME_EXCLUDED_SKILLS
    ]
    required_names = set(required)
    advisory = [
        skill
        for skill in _unique_strings(skill_tiers["advisory"])
        if skill not in required_names and skill not in COMPACT_RESUME_EXCLUDED_SKILLS
    ]
    return {"required": required, "advisory": advisory}


def _extend_unique_strings(target: list[str], values: Any) -> None:
    for value in _iter_strings(values):
        if value not in target:
            target.append(value)


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    _extend_unique_strings(unique, values)
    return unique


def _iter_strings(values: Any) -> list[str]:
    if isinstance(values, str):
        return [values] if values else []
    if not isinstance(values, list | tuple | set):
        return []

    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value:
            result.append(value)
    return result


def _remove_session_variable(db: HubDatabase, session_id: str, name: str) -> Any:
    return _pop_session_variable(db, session_id, name)


def _pop_session_variable(
    db: HubDatabase,
    session_id: str,
    name: str,
    *,
    expected_attempt_id: str | None = None,
) -> Any:
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        variables = _load_variables(_row_variables(row))
        current_value = variables.get(name)
        if expected_attempt_id is not None and (
            not isinstance(current_value, dict)
            or current_value.get("attempt_id") != expected_attempt_id
        ):
            return None
        value = variables.pop(name, None)
        if value is not None:
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
                (json.dumps(variables), now, session_id),
            )
        return value


def _row_variables(row: Any) -> Any:
    if row is None:
        return None
    try:
        return row["variables"]
    except Exception:
        return None


def _load_variables(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, bytes):
        raw = raw.decode()
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw[:80].replace("\n", "\\n")
        logger.warning(
            "Corrupt set_handoff compact continuation variables JSON ignored: %s; preview=%r",
            exc,
            preview,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
