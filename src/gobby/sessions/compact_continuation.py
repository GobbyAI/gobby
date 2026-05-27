"""Continuation scheduling for Gobby-initiated terminal compactions."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.sessions.tmux_context import get_tmux_manager_for_context, parse_terminal_context_value

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

COMPACT_SELF_CONTINUE_VARIABLE = "compact_self_continue_pending"
COMPACT_SELF_CONTINUE_PROMPT = "Continue where you last left off."
COMPACT_SELF_CONTINUE_FRESH_SECONDS = 600
COMPACT_SELF_CONTINUE_SEND_DELAY_SECONDS = 1.0
COMPACT_SELF_CONTINUE_FALLBACK_DELAY_SECONDS = 3.0


def mark_compact_self_continuation_pending(
    db: HubDatabase,
    session_id: str,
    *,
    prompt: str = COMPACT_SELF_CONTINUE_PROMPT,
    now: datetime | None = None,
) -> bool:
    """Store the pending continuation marker on the compacting session."""
    payload = {
        "prompt": prompt,
        "created_at": _format_timestamp(now or datetime.now(UTC)),
    }
    try:
        _merge_session_variable(db, session_id, COMPACT_SELF_CONTINUE_VARIABLE, payload)
        return True
    except Exception:
        logger.warning(
            "Failed to mark compact_self continuation pending for session %s",
            session_id,
            exc_info=True,
        )
        return False


def clear_compact_self_continuation_pending(db: HubDatabase, session_id: str) -> bool:
    """Clear the pending continuation marker if it exists."""
    try:
        _remove_session_variable(db, session_id, COMPACT_SELF_CONTINUE_VARIABLE)
        return True
    except Exception:
        logger.warning(
            "Failed to clear compact_self continuation pending for session %s",
            session_id,
            exc_info=True,
        )
        return False


def consume_compact_self_continuation_pending(
    db: HubDatabase,
    session_id: str,
    *,
    now: datetime | None = None,
    fresh_seconds: int = COMPACT_SELF_CONTINUE_FRESH_SECONDS,
) -> str | None:
    """Consume a fresh pending marker and return its prompt."""
    try:
        value = _pop_session_variable(db, session_id, COMPACT_SELF_CONTINUE_VARIABLE)
    except Exception:
        logger.warning(
            "Failed to consume compact_self continuation pending for session %s",
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
    return prompt if isinstance(prompt, str) and prompt.strip() else COMPACT_SELF_CONTINUE_PROMPT


def schedule_compact_self_continuation(
    session: Any,
    prompt: str,
    *,
    loop: Any | None = None,
    delay_seconds: float = COMPACT_SELF_CONTINUE_SEND_DELAY_SECONDS,
) -> bool:
    """Schedule a best-effort tmux prompt send without blocking SessionStart."""
    session_id = getattr(session, "id", "unknown")
    ctx = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if ctx is None:
        logger.debug("Cannot schedule compact_self continuation; no terminal context")
        return False

    target = ctx.get("tmux_pane") or ctx.get("tmux_session")
    if not target:
        logger.debug(
            "Cannot schedule compact_self continuation for session %s; no tmux target",
            session_id,
        )
        return False

    tmux = get_tmux_manager_for_context(ctx)
    coro = _send_compact_self_continuation(
        tmux,
        str(target),
        prompt,
        str(session_id),
        delay_seconds=delay_seconds,
    )
    return _schedule_coroutine(coro, loop=loop)


def consume_and_schedule_compact_self_continuation(
    db: HubDatabase,
    *,
    pending_session_id: str | None,
    target_session: Any,
    fallback_pending_session_id: str | None = None,
    loop: Any | None = None,
) -> bool:
    """Consume a fresh marker from one session and schedule the prompt on another."""
    prompt = None
    if pending_session_id:
        prompt = consume_compact_self_continuation_pending(db, pending_session_id)
    if prompt is None and fallback_pending_session_id != pending_session_id:
        if fallback_pending_session_id:
            prompt = consume_compact_self_continuation_pending(db, fallback_pending_session_id)
    if prompt is None:
        return False
    return schedule_compact_self_continuation(target_session, prompt, loop=loop)


def schedule_compact_self_continuation_fallback(
    db: HubDatabase,
    *,
    pending_session_id: str,
    target_session: Any,
    loop: Any | None = None,
    delay_seconds: float = COMPACT_SELF_CONTINUE_FALLBACK_DELAY_SECONDS,
) -> bool:
    """Schedule a delayed fallback for CLIs that do not emit SessionStart after compacting."""
    coro = _consume_and_send_compact_self_continuation_fallback(
        db,
        pending_session_id=pending_session_id,
        target_session=target_session,
        delay_seconds=delay_seconds,
    )
    return _schedule_coroutine(coro, loop=loop)


async def _send_compact_self_continuation(
    tmux: Any,
    target: str,
    prompt: str,
    session_id: str,
    *,
    delay_seconds: float,
) -> None:
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    try:
        ok = await tmux.send_keys(target, f"{prompt}\n", literal=True)
    except Exception:
        logger.warning(
            "Failed to send compact_self continuation prompt for session %s",
            session_id,
            exc_info=True,
        )
        return
    if not ok:
        logger.warning("tmux send-keys returned false for compact_self continuation %s", session_id)


async def _consume_and_send_compact_self_continuation_fallback(
    db: HubDatabase,
    *,
    pending_session_id: str,
    target_session: Any,
    delay_seconds: float,
) -> None:
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    session_id = getattr(target_session, "id", pending_session_id)
    ctx = parse_terminal_context_value(getattr(target_session, "terminal_context", None))
    if ctx is None:
        logger.debug("Cannot run compact_self continuation fallback; no terminal context")
        return

    target = ctx.get("tmux_pane") or ctx.get("tmux_session")
    if not target:
        logger.debug(
            "Cannot run compact_self continuation fallback for session %s; no tmux target",
            session_id,
        )
        return

    prompt = consume_compact_self_continuation_pending(db, pending_session_id)
    if prompt is None:
        return

    tmux = get_tmux_manager_for_context(ctx)
    await _send_compact_self_continuation(
        tmux,
        str(target),
        prompt,
        str(session_id),
        delay_seconds=0,
    )


def _schedule_coroutine(coro: Any, *, loop: Any | None = None) -> bool:
    try:
        running_loop = asyncio.get_running_loop()
        # Fire-and-forget: the coroutine logs its own failures and must not block startup.
        running_loop.create_task(coro)
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
                logger.debug("Failed to schedule compact_self continuation on loop", exc_info=True)
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
        logger.debug("Failed to start compact_self continuation thread", exc_info=True)
        # The fallback thread never took ownership, so close the coroutine explicitly.
        coro.close()
        return False
    return True


def _run_coroutine_thread(coro: Any) -> None:
    try:
        asyncio.run(coro)
    except Exception:
        logger.debug("Failed to run compact_self continuation task", exc_info=True)


def _merge_session_variable(
    db: HubDatabase,
    session_id: str,
    name: str,
    value: Any,
) -> None:
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate() as conn:
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


def _remove_session_variable(db: HubDatabase, session_id: str, name: str) -> Any:
    return _pop_session_variable(db, session_id, name)


def _pop_session_variable(db: HubDatabase, session_id: str, name: str) -> Any:
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate() as conn:
        row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        variables = _load_variables(_row_variables(row))
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
            "Corrupt compact_self continuation variables JSON ignored: %s; preview=%r",
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
