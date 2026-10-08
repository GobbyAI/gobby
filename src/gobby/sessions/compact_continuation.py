"""Continuation scheduling for Gobby-initiated terminal compactions."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.idle_detector import IdleDetector
from gobby.sessions.compact_continuation_store import (
    _fail_delivered_readiness,
    _format_timestamp,
    _load_session_variables,
    _merge_session_variable,
    _parse_timestamp,
    _pop_session_variable,
    _restore_session_variable_if_absent,
)
from gobby.sessions.compact_markers import (
    COMPACT_HANDOFF_MARKER_VARIABLE,
    HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS,
    HANDOFF_COMPACT_CONTINUE_SEND_DELAY_SECONDS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
)
from gobby.sessions.continuation_retry import (
    continuation_write_refusal,
    resubmit_until_before_agent,
    turn_lifecycle_generation,
)
from gobby.sessions.handoff import build_handoff_continue_prompt
from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.sessions.handoff_records import record_handoff_delivery
from gobby.sessions.tmux_context import parse_terminal_context_value
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_models import Session
from gobby.terminals.composer_lock import composer_action_lock
from gobby.terminals.pane_io import (
    COMPOSER_UNKNOWN_ERROR_CODE,
    ENTER_DELIVERY_UNCONFIRMED_ERROR_CODE,
    SUBMIT_UNVERIFIED_ERROR_CODE,
    SUBMIT_VERIFY_SECONDS,
    ComposerReader,
    SubmitResult,
    clear_composer,
    composer_gate_for_write,
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
_COMPACT_BOUNDARY_WAITERS: dict[str, CompactBoundaryWaiter] = {}
_COMPACT_BOUNDARY_WAITERS_LOCK = threading.Lock()


@dataclass
class CompactBoundaryWaiter:
    """One delivery operation awaiting the provider's compact hook boundary."""

    attempt_id: str
    handoff_record_id: str
    terminal_context: Any
    event: asyncio.Event
    loop: asyncio.AbstractEventLoop
    submitted: bool = False


def register_compact_boundary_waiter(
    session_id: str, attempt_id: str, handoff_record_id: str, terminal_context: Any
) -> CompactBoundaryWaiter:
    waiter = CompactBoundaryWaiter(
        attempt_id, handoff_record_id, terminal_context, asyncio.Event(), asyncio.get_running_loop()
    )
    with _COMPACT_BOUNDARY_WAITERS_LOCK:
        if session_id in _COMPACT_BOUNDARY_WAITERS:
            raise RuntimeError(f"compact boundary wait already active for session {session_id}")
        _COMPACT_BOUNDARY_WAITERS[session_id] = waiter
    return waiter


def arm_compact_boundary_waiter(session_id: str, attempt_id: str) -> None:
    """Accept boundary evidence only once this attempt is submitting its command."""
    with _COMPACT_BOUNDARY_WAITERS_LOCK:
        waiter = _COMPACT_BOUNDARY_WAITERS.get(session_id)
        if waiter is not None and waiter.attempt_id == attempt_id:
            waiter.submitted = True


def compact_boundary_wait_submitted(session_id: str) -> bool:
    """Return whether a delivery operation already watches this session's compact."""
    with _COMPACT_BOUNDARY_WAITERS_LOCK:
        waiter = _COMPACT_BOUNDARY_WAITERS.get(session_id)
        return waiter is not None and waiter.submitted


def disarm_compact_boundary_waiter(session_id: str, attempt_id: str) -> None:
    """Stop accepting boundaries while a failed submission awaits its retry."""
    with _COMPACT_BOUNDARY_WAITERS_LOCK:
        waiter = _COMPACT_BOUNDARY_WAITERS.get(session_id)
        if waiter is not None and waiter.attempt_id == attempt_id:
            waiter.submitted = False


def unregister_compact_boundary_waiter(session_id: str, attempt_id: str) -> None:
    with _COMPACT_BOUNDARY_WAITERS_LOCK:
        waiter = _COMPACT_BOUNDARY_WAITERS.get(session_id)
        if waiter is not None and waiter.attempt_id == attempt_id:
            del _COMPACT_BOUNDARY_WAITERS[session_id]


def notify_compact_boundary(db: HubDatabase, session_id: str, terminal_context: Any) -> None:
    """Receipt and wake the unique submitted handoff on this terminal process."""
    with _COMPACT_BOUNDARY_WAITERS_LOCK:
        matches = [
            waiter
            for pending_id, waiter in _COMPACT_BOUNDARY_WAITERS.items()
            if waiter.submitted
            and (
                pending_id == session_id
                or terminal_process_contexts_match(waiter.terminal_context, terminal_context)
            )
        ]
        if len(matches) == 1:
            waiter = matches[0]
            try:
                # Selection and receipt must be atomic with unregister. Otherwise
                # timeout compensation can remove the staged handoff before this
                # insert, even though the provider boundary already happened.
                record_handoff_delivery(
                    db,
                    handoff_id=waiter.handoff_record_id,
                    attempt_id=waiter.attempt_id,
                    boundary_kind="compact",
                    continuation_session_id=session_id,
                )
            except Exception:
                logger.warning(
                    "Failed recording compact boundary for session %s attempt %s",
                    session_id,
                    waiter.attempt_id,
                    exc_info=True,
                )
                return
            waiter.loop.call_soon_threadsafe(waiter.event.set)


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
        db=db,
    )
    return _schedule_coroutine(coro, loop=loop)


def _continuation_pane(
    session: Any,
    session_id: str,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> PaneIO | None:
    """Use a live native row or a bound native terminal context."""
    from gobby.terminals.pane_io import context_runtime_pane, live_runtime_pane

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
    logger.warning(
        "Cannot schedule set_handoff compact continuation for session %s; no live terminal",
        session_id,
    )
    return None


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
    notify_compact_boundary(
        db, pending_session_id, getattr(target_session, "terminal_context", None)
    )
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
    try:
        scheduled = schedule_handoff_compact_continuation(
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
        )
    except Exception:
        logger.warning(
            "Failed scheduling compact continuation for session %s",
            target_session_id,
            exc_info=True,
        )
        scheduled = False
    if scheduled:
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
    if len(matching) > 1:
        logger.warning(
            "Skipping set_handoff continuation for %s: %d same-terminal markers are ambiguous",
            pending_session_id,
            len(matching),
        )
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
    db: HubDatabase | None = None,
) -> bool:
    """Type the pull prompt; with ``db``, confirm it by the session's BEFORE_AGENT.

    A composer read that reported the draft left can be a false positive (the
    22:28:40 CDT read on 2026-09-21), so a typed prompt is not delivery. When a
    hub database is supplied the continuation is re-submitted until the
    BEFORE_AGENT it triggers arrives or the bounded budget is spent, then durable
    fallback delivers it exactly once. When the hub database is supplied but the
    session's turn lifecycle cannot be read, a screen-verified type is not
    delivery: the unreadable proof cannot confirm BEFORE_AGENT, so the caller
    takes the durable fallback instead of reporting a composer-only success.
    Without a hub database there is no lifecycle to read at all and the single
    verified submit the CLI already acked stands.
    """
    baseline: int | None = None
    lifecycle_unreadable = False
    if db is not None:
        # Read the turn generation before typing: the continuation prompt's own
        # BEFORE_AGENT is what bumps it, so anything above this baseline proves
        # the prompt reached the CLI.
        try:
            baseline = await asyncio.to_thread(turn_lifecycle_generation, db, session_id)
        except Exception:
            baseline = None
        lifecycle_unreadable = baseline is None
    # The pull prompt is typed into the same physical composer a wake drains and
    # submits, so hold the shared lock across its whole clear/submit/verify run and
    # the BEFORE_AGENT re-submit ladder that may type it again.
    async with composer_action_lock(str(getattr(pane, "target", "") or "")):
        result = await _type_handoff_compact_continuation(
            pane,
            prompt,
            session_id,
            delay_seconds=delay_seconds,
            cli_source=cli_source,
            composer_read=composer_read,
            verify_seconds=SUBMIT_VERIFY_SECONDS,
            db=db,
        )
        sent = result.ok
        if (
            (
                sent
                or result.error_code
                in {SUBMIT_UNVERIFIED_ERROR_CODE, ENTER_DELIVERY_UNCONFIRMED_ERROR_CODE}
            )
            and db is not None
            and baseline is not None
        ):
            sent = await resubmit_until_before_agent(
                pane,
                prompt,
                session_id,
                db=db,
                baseline_generation=baseline,
                cli_source=cli_source,
                composer_read=composer_read,
                verify_seconds=SUBMIT_VERIFY_SECONDS,
            )
    if lifecycle_unreadable:
        # An unreadable lifecycle can neither confirm nor refute BEFORE_AGENT, so
        # the composer read is not delivery. Report unconfirmed and let the
        # caller queue the durable pull prompt instead of trusting the screen.
        logger.warning(
            "Unconfirmed set_handoff continuation for %s: turn lifecycle is unreadable",
            session_id,
        )
        sent = False
    if not sent and on_send_failure is not None:
        on_send_failure()
    return sent


#: Composer reads before an unclassifiable frame refuses the pull prompt, one
#: verify interval apart: enough for Claude's post-compact redraw to settle.
CONTINUATION_COMPOSER_PROBES = 3


def _refuse_continuation(session_id: str, reason: str) -> SubmitResult:
    logger.warning(
        "Skipping set_handoff continuation for %s: %s",
        session_id,
        reason,
        extra={"event": "handoff_continuation_refused", "session_id": session_id},
    )
    return SubmitResult(False, reason)


async def _type_handoff_compact_continuation(
    pane: PaneIO,
    prompt: str,
    session_id: str,
    *,
    delay_seconds: float,
    cli_source: str | None,
    composer_read: ComposerReader | None,
    verify_seconds: float,
    db: HubDatabase | None = None,
) -> SubmitResult:
    """Type the pull prompt and prove it left the composer, or report the failure.

    The prompt gets the same verified-submit ladder as the compaction command that
    precedes it: a Delivered Enter is not a submitted prompt, so the composer is read
    back after every Enter. A held prompt gets bare-Enter retries without being
    retyped. An unverified submit retains its text and error code so the caller can
    await BEFORE_AGENT before considering any retry. A proven held draft is drained
    after its retry budget, then reported for durable fallback.
    """
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    try:
        if db is not None and (
            refusal := await asyncio.to_thread(continuation_write_refusal, db, session_id)
        ):
            return _refuse_continuation(session_id, refusal)
        # An operator draft in the composer would be submitted with the pull
        # prompt, so require an empty composer or the exact pending prompt.
        # Only an unprobed composer keeps the blind drain: after a confirmed-empty
        # read it could only delete keystrokes the operator typed since.
        # SessionStart(compact) can arrive before Claude redraws its composer, so an
        # unclassifiable frame is re-read after it settles; foreign drafts are refused.
        for probe in range(1, CONTINUATION_COMPOSER_PROBES + 1):
            writable, refuse_reason, composer_state = await composer_gate_for_write(
                pane,
                cli_source,
                composer_read,
                action="the set_handoff continuation",
                pending_payload=prompt,
            )
            if writable or composer_state != "unknown" or probe == CONTINUATION_COMPOSER_PROBES:
                break
            await asyncio.sleep(verify_seconds)
        if not writable:
            return _refuse_continuation(
                session_id, f"{refuse_reason} (composer {composer_state} after {probe} probe(s))"
            )
        ok, reason = (
            (True, None)
            if composer_state in {"empty", "held"}
            else await clear_composer(pane, cli_source)
        )
        if not ok:
            logger.warning(
                "Failed clearing the composer before set_handoff continuation for %s: %s",
                session_id,
                reason,
            )
            return SubmitResult(False, reason)
        if db is not None and (
            refusal := await asyncio.to_thread(continuation_write_refusal, db, session_id)
        ):
            return _refuse_continuation(session_id, refusal)
        result = await submit_text(
            pane,
            prompt,
            session_id,
            label="the set_handoff continuation prompt",
            cli_source=cli_source,
            composer_read=composer_read,
            verify_seconds=verify_seconds,
            pending_payload=prompt,
        )
        if result.ok:
            return result
        if result.error_code in {
            "composer_occupied",
            COMPOSER_UNKNOWN_ERROR_CODE,
            SUBMIT_UNVERIFIED_ERROR_CODE,
            ENTER_DELIVERY_UNCONFIRMED_ERROR_CODE,
        }:
            # A protected refusal or uncertain Enter must preserve the composer:
            # clearing now could erase operator text.
            return result
        logger.error(
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
            logger.error(
                "Composer still holds the unsubmitted continuation prompt for session %s: %s",
                session_id,
                clear_reason,
            )
        return result
    except Exception:
        logger.warning(
            "Failed to send set_handoff compact continuation prompt for session %s",
            session_id,
            exc_info=True,
        )
    return SubmitResult(False, "continuation write failed")


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
    """Submit after a matching compact receipt, or a fresh completion marker."""
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
            return
        if not isinstance(output, str):
            logger.debug(
                "Stopped Codex compact readiness watcher after pane disappeared for session %s",
                pending_session_id,
            )
            return

        receipt = None
        if attempt_id is not None:
            receipt = await asyncio.to_thread(
                db.fetchone,
                "SELECT 1 FROM session_handoff_deliveries WHERE attempt_id = %s "
                "AND boundary_kind = 'compact'",
                (attempt_id,),
            )
        fresh_output = _fresh_terminal_output(before_command, output)
        ready = (
            receipt is not None
            or _count_codex_compact_ready_status_lines(output) > baseline_count
            or _count_codex_compact_ready_status_lines(fresh_output) > 0
        )
        if ready:
            if attempt_id is not None and receipt is None:
                # The provider can render its status before the delivery receipt.
                # The delivery waiter restarts readiness after recording it.
                return
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
                db=db,
            )
            return

        if poll_seconds > 0:
            await asyncio.sleep(poll_seconds)

    logger.warning(
        "Timed out waiting for Codex compact readiness for session %s",
        pending_session_id,
    )
    if attempt_id is not None:
        await asyncio.to_thread(_fail_delivered_readiness, db, pending_session_id, attempt_id)


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
