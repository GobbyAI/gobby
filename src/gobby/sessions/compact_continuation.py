"""Continuation scheduling for Gobby-initiated terminal compactions."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.idle_detector import IdleDetector
from gobby.sessions.compact_markers import (
    COMPACT_HANDOFF_MARKER_VARIABLE,
    HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS,
    HANDOFF_COMPACT_CONTINUE_SEND_DELAY_SECONDS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
)
from gobby.sessions.handoff import HANDOFF_DISPATCH_GATE_VARIABLE, build_handoff_continue_prompt
from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.sessions.tmux_context import parse_terminal_context_value
from gobby.storage.hub.protocol import SessionVariableMutation
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_models import Session
from gobby.terminals.lookup import manager_for_terminal_context
from gobby.terminals.pane_io import (
    SUBMIT_VERIFY_SECONDS,
    ComposerReader,
    clear_composer,
    submit_text,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.terminals.pane_io import PaneIO

__all__ = [
    "COMPACT_HANDOFF_MARKER_VARIABLE",
    "HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS",
    "HANDOFF_COMPACT_CONTINUE_SEND_DELAY_SECONDS",
    "HANDOFF_COMPACT_CONTINUE_VARIABLE",
    "persist_pull_prompt_message",
]

logger = logging.getLogger(__name__)

_HANDOFF_COMPACT_CONTINUATION_TASKS: set[asyncio.Task[Any]] = set()

_CODEX_COMPACT_READY_STATUS_LINE = "• Context compacted"
_CODEX_COMPACT_READY_POLL_SECONDS = 0.25
CODEX_COMPACT_READY_CAPTURE_LINES = 100


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
    db: HubDatabase | None = None,
    on_send_failure: Callable[[], None] | None = None,
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
) -> bool:
    """Schedule a best-effort prompt send without blocking SessionStart."""
    session_id = str(getattr(session, "id", "unknown"))
    pane = _continuation_pane(session, session_id, terminal_manager, terminal_runtime_registry)
    if pane is None:
        return False
    cli_source = getattr(session, "source", None)
    coro = _send_handoff_compact_continuation(
        pane,
        prompt,
        session_id,
        delay_seconds=delay_seconds,
        cli_source=cli_source,
        on_send_failure=on_send_failure,
        composer_read=_composer_reader(db, cli_source),
    )
    return _schedule_coroutine(coro, loop=loop)


def _continuation_pane(
    session: Any,
    session_id: str,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> PaneIO | None:
    """Route like compaction delivery: live row, unbound gterm context, else tmux."""
    from gobby.terminals.pane_io import TmuxPaneIO, context_runtime_pane, live_runtime_pane

    try:
        pane = live_runtime_pane(session_id, terminal_manager, terminal_runtime_registry)
        if pane is None:
            pane = context_runtime_pane(session, terminal_manager, terminal_runtime_registry)
    except Exception:
        logger.warning(
            "Failed resolving the live terminal for set_handoff compact continuation %s",
            session_id,
            exc_info=True,
        )
        pane = None
    if pane is not None:
        return pane
    ctx = parse_terminal_context_value(getattr(session, "terminal_context", None))
    target = None if ctx is None else ctx.get("tmux_pane") or ctx.get("tmux_session")
    if not target:
        logger.warning(
            "Cannot schedule set_handoff compact continuation for session %s; "
            "no live terminal or tmux target",
            session_id,
        )
        return None
    return TmuxPaneIO(manager_for_terminal_context(ctx), str(target))


def _composer_reader(db: HubDatabase | None, cli_source: str | None) -> ComposerReader | None:
    if db is None or not cli_source:
        return None
    detector = IdleDetector(DetectionManifestRegistry(db), cli_source)
    return detector.composer_read if detector.reads_composer() else None


def persist_pull_prompt_message(
    db: HubDatabase,
    session_id: str,
    prompt: str,
    attempt_id: str | None,
) -> None:
    """Queue the pull prompt as a self-addressed ISM when typing it failed.

    The hook piggyback injects undelivered messages on the session's next
    turn, so the operator's next submit still pulls the handoff.
    """
    try:
        InterSessionMessageManager(db).create_message(
            from_session=session_id,
            to_session=session_id,
            content=prompt,
            message_type="handoff_continuation",
            metadata_json=json.dumps(
                {"attempt_id": attempt_id, "compact_continuation": True},
                sort_keys=True,
            ),
        )
    except Exception:
        logger.warning(
            "Failed to queue the handoff pull prompt for session %s after a send failure",
            session_id,
            exc_info=True,
        )


def schedule_codex_handoff_compact_continuation_readiness(
    db: HubDatabase,
    *,
    pane: PaneIO,
    pending_session_id: str,
    before_command: str | None,
    attempt_id: str | None = None,
    loop: Any | None = None,
    poll_seconds: float = _CODEX_COMPACT_READY_POLL_SECONDS,
) -> bool:
    """Wait for Codex's terminal completion signal on the compacted pane, then continue."""
    if before_command is None:
        logger.debug(
            "Cannot schedule Codex compact readiness for session %s; baseline capture failed",
            pending_session_id,
        )
        return False

    coro = _continue_after_codex_compaction_ready(
        db,
        pane=pane,
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
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
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
    attempt_id = payload.get("attempt_id") if isinstance(payload, dict) else None
    target_session_id = str(getattr(target_session, "id", source_session_id))
    if schedule_handoff_compact_continuation(
        target_session,
        prompt,
        loop=loop,
        db=db,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
        on_send_failure=partial(
            persist_pull_prompt_message,
            db,
            target_session_id,
            prompt,
            str(attempt_id) if attempt_id is not None else None,
        ),
    ):
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
            SELECT s.*, p.name AS project_name
              FROM sessions s
              LEFT JOIN projects p ON p.id = s.project_id
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
    pane: PaneIO,
    prompt: str,
    session_id: str,
    *,
    delay_seconds: float,
    cli_source: str | None = None,
    on_send_failure: Callable[[], None] | None = None,
    composer_read: ComposerReader | None = None,
) -> bool:
    sent = await _type_handoff_compact_continuation(
        pane,
        prompt,
        session_id,
        delay_seconds=delay_seconds,
        cli_source=cli_source,
        composer_read=composer_read,
        verify_seconds=SUBMIT_VERIFY_SECONDS,
    )
    if not sent and on_send_failure is not None:
        on_send_failure()
    return sent


async def _type_handoff_compact_continuation(
    pane: PaneIO,
    prompt: str,
    session_id: str,
    *,
    delay_seconds: float,
    cli_source: str | None,
    composer_read: ComposerReader | None,
    verify_seconds: float,
) -> bool:
    """Type the pull prompt and prove it left the composer, or report the failure.

    The prompt gets the same verified-submit ladder as the compaction command that
    precedes it: a Delivered Enter is not a submitted prompt, so the composer is read
    back after every Enter. A held prompt gets bare-Enter retries without being
    retyped. If it never leaves, the caller drains the draft before its durable
    fallback delivers it exactly once. A drain that itself fails still reports the
    failure: a duplicated prompt is a far smaller harm than a lost handoff.
    """
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    try:
        # An operator draft in the composer would be submitted with the pull
        # prompt, so empty the box first (blind: the prompt reads fine regardless).
        ok, reason = await clear_composer(pane, cli_source)
        if not ok:
            logger.warning(
                "Failed clearing the composer before set_handoff continuation for %s: %s",
                session_id,
                reason,
            )
            return False
        result = await submit_text(
            pane,
            prompt,
            session_id,
            label="the set_handoff continuation prompt",
            cli_source=cli_source,
            composer_read=composer_read,
            verify_seconds=verify_seconds,
        )
        if result.ok:
            return True
        logger.warning(
            "Failed to submit the set_handoff compact continuation prompt for session %s: %s",
            session_id,
            result.reason,
            extra={
                "event": "handoff_continuation_not_submitted",
                "session_id": session_id,
                "error_code": result.error_code,
            },
        )
        cleared, clear_reason = await clear_composer(pane, cli_source)
        if not cleared:
            logger.warning(
                "Composer still holds the unsubmitted continuation prompt for session %s: %s",
                session_id,
                clear_reason,
            )
    except Exception:
        logger.warning(
            "Failed to send set_handoff compact continuation prompt for session %s",
            session_id,
            exc_info=True,
        )
    return False


async def _continue_after_codex_compaction_ready(
    db: HubDatabase,
    *,
    pane: PaneIO,
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
            output = await pane.snapshot(CODEX_COMPACT_READY_CAPTURE_LINES)
        except Exception:
            logger.debug(
                "Failed to inspect Codex compact readiness for session %s",
                pending_session_id,
                exc_info=True,
            )
            await asyncio.to_thread(
                _fail_codex_compact_readiness, db, pending_session_id, attempt_id
            )
            return
        if not isinstance(output, str):
            logger.debug(
                "Stopped Codex compact readiness watcher after pane disappeared for session %s",
                pending_session_id,
            )
            await asyncio.to_thread(
                _fail_codex_compact_readiness, db, pending_session_id, attempt_id
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
            # A restored marker has no consumer inside the readiness window, so
            # a failed send queues the prompt for the hook piggyback instead.
            await _send_handoff_compact_continuation(
                pane,
                prompt,
                pending_session_id,
                delay_seconds=0,
                cli_source="codex",
                on_send_failure=partial(
                    persist_pull_prompt_message, db, pending_session_id, prompt, attempt_id
                ),
                composer_read=_composer_reader(db, "codex"),
            )
            return

        if poll_seconds > 0:
            await asyncio.sleep(poll_seconds)

    logger.warning(
        "Timed out waiting for Codex compact readiness for session %s",
        pending_session_id,
    )
    await asyncio.to_thread(_fail_codex_compact_readiness, db, pending_session_id, attempt_id)


def _fail_codex_compact_readiness(db: HubDatabase, session_id: str, attempt_id: str | None) -> bool:
    """Release only the timed-out attempt's gate; a newer attempt owns its own state."""
    if attempt_id is None:
        return False
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s", (session_id,)
        ).fetchone()
        if row is None:
            return False
        variables = _load_variables(_row_variables(row))
        marker = variables.get(HANDOFF_COMPACT_CONTINUE_VARIABLE)
        gate = variables.get(HANDOFF_DISPATCH_GATE_VARIABLE)
        if (
            not isinstance(marker, dict)
            or marker.get("attempt_id") != attempt_id
            or not isinstance(gate, dict)
            or gate.get("attempt_id") != attempt_id
            or gate.get("delivery_pending") is not True
        ):
            return False
        variables.pop(HANDOFF_COMPACT_CONTINUE_VARIABLE)
        variables[HANDOFF_DISPATCH_GATE_VARIABLE] = {
            "compacted": False,
            "delivery_pending": False,
            "delivery_failed": True,
            "attempt_id": attempt_id,
            "clear_session": False,
            "reason": "Codex compact readiness timed out before continuation delivery",
            "error_code": "compact_readiness_timeout",
            "retry_guidance": "Retry gobby-sessions:set_handoff after compact readiness failed.",
        }
        conn.execute(
            "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
            (json.dumps(variables), now, session_id),
        )
    return True


def _count_codex_compact_ready_status_lines(output: str) -> int:
    """Count complete Codex compaction status lines."""
    return sum(
        line.strip().startswith(_CODEX_COMPACT_READY_STATUS_LINE) for line in output.splitlines()
    )


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


async def shutdown_compact_continuations() -> None:
    """Stop this daemon loop's prompt senders and readiness watchers before DB close."""
    loop = asyncio.get_running_loop()
    tasks = [task for task in _HANDOFF_COMPACT_CONTINUATION_TASKS if task.get_loop() is loop]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _schedule_coroutine(coro: Any, *, loop: Any | None = None) -> bool:
    try:
        running_loop = asyncio.get_running_loop()
        # Retain ownership until completion so shutdown can stop DB-backed watchers.
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
                # Create and retain the task on its owner loop, including calls
                # dispatched from synchronous MCP/tool workers.
                loop.call_soon_threadsafe(_schedule_coroutine, coro)
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
