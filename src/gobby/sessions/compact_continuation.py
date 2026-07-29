"""Continuation scheduling for Gobby-initiated terminal compactions."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.sessions.tmux_context import get_tmux_manager_for_context, parse_terminal_context_value
from gobby.skills.formatting import skill_fetch_batch_directive
from gobby.storage.hub.protocol import SessionVariableMutation
from gobby.utils.injected_context import INJECTED_CONTEXT_BEGIN

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

_COMPACT_SELF_CONTINUATION_TASKS: set[asyncio.Task[Any]] = set()

COMPACT_SELF_CONTINUE_VARIABLE = "compact_self_continue_pending"
COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE = "compact_resume_required_skills"
COMPACT_HANDOFF_MARKER_VARIABLE = "handoff_source"
_COMPACT_SELF_CONTINUE_INTRO = (
    "Continue where you last left off. If the previous turn shows a rejected or "
    "cancelled compact_self tool-use message immediately followed by /compact or "
    "/compress, treat it as expected terminal self-compaction delivery, not user "
    "refusal. "
)
COMPACT_SELF_CONTINUE_PROMPT = (
    _COMPACT_SELF_CONTINUE_INTRO + "If startup context contains "
    f"`{INJECTED_CONTEXT_BEGIN}`, use that injected context directly and continue. "
    "Only if the injected context is missing or incomplete, call "
    "`gobby-sessions.wait_for_summary` for the compacted session. If it returns "
    "`completed=false`, repeat the same wait call. Once complete, use the returned "
    "`context` and continue."
)
COMPACT_SELF_CONTINUE_FRESH_SECONDS = 600
COMPACT_SELF_CONTINUE_SEND_DELAY_SECONDS = 1.0
_CODEX_COMPACT_READY_MARKER = "Context compacted"
_CODEX_COMPACT_READY_POLL_SECONDS = 0.25
CODEX_COMPACT_READY_CAPTURE_LINES = 100
LOADING_SKILLS_NAME = "loading-skills"
COMPACT_RESUME_SKILL_VARIABLE_KEYS = (
    "required_skills",
    "additional_skills",
    "claimed_task_required_skills",
    "claimed_task_additional_skills",
)


def mark_compact_self_continuation_pending(
    db: HubDatabase,
    session_id: str,
    *,
    prompt: str = COMPACT_SELF_CONTINUE_PROMPT,
    summary_session_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Store the pending continuation marker on the compacting session."""
    payload = {
        "prompt": prompt,
        "created_at": _format_timestamp(now or datetime.now(UTC)),
    }
    if summary_session_id:
        payload["summary_session_id"] = summary_session_id
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


def persist_compact_resume_required_skills(
    db: HubDatabase,
    session_id: str,
) -> list[str]:
    """Persist skills that should be reloaded after compact_self resumes."""
    variables = _load_session_variables(db, session_id)
    skills = _collect_compact_resume_required_skills(variables)
    try:
        _merge_session_variable(db, session_id, COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE, skills)
    except Exception:
        logger.warning(
            "Failed to persist compact resume required skills for session %s",
            session_id,
            exc_info=True,
        )
    return skills


def build_compact_self_continue_prompt(
    required_skills: list[str] | None,
    *,
    summary_session_id: str | None = None,
) -> str:
    """Build the post-compact continuation prompt with skill reload directives."""
    wait_directive = _build_wait_for_summary_directive(summary_session_id)
    skills = _prepare_compact_resume_skills(required_skills or [])
    if not skills:
        return wait_directive

    directives = skill_fetch_batch_directive(skills)
    return (
        f"{wait_directive}\n\n"
        "Before continuing the task, reload these required skills directly in order:\n\n"
        f"{directives}"
    )


def consume_compact_handoff_marker(db: HubDatabase, session_id: str) -> bool:
    """Consume the one-shot compact marker after successful in-place reactivation.

    The marker (session variable ``handoff_source``) is written by the
    pre-compact rule and read by session-start classification; consuming it
    here keeps ordinary later restarts from classifying as compact and lets
    the stale-compact retention sweep skip resumed sessions.
    """
    return _pop_session_variable(db, session_id, COMPACT_HANDOFF_MARKER_VARIABLE) is not None


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
    pending = _take_compact_self_continuation_pending(
        db,
        session_id,
        now=now,
        fresh_seconds=fresh_seconds,
    )
    return pending[0] if pending is not None else None


def _take_compact_self_continuation_pending(
    db: HubDatabase,
    session_id: str,
    *,
    now: datetime | None = None,
    fresh_seconds: int = COMPACT_SELF_CONTINUE_FRESH_SECONDS,
) -> tuple[str, dict[str, Any]] | None:
    """Atomically take a fresh marker while retaining its exact payload."""
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
    resolved_prompt = (
        prompt if isinstance(prompt, str) and prompt.strip() else COMPACT_SELF_CONTINUE_PROMPT
    )
    return resolved_prompt, value


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


def schedule_codex_compact_self_continuation_readiness(
    db: HubDatabase,
    *,
    pending_session_id: str,
    target_session: Any,
    before_command: str | None,
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

    tmux = get_tmux_manager_for_context(ctx)
    coro = _continue_after_codex_compaction_ready(
        db,
        tmux=tmux,
        target=str(target),
        pending_session_id=pending_session_id,
        before_command=before_command,
        poll_seconds=poll_seconds,
    )
    return _schedule_coroutine(coro, loop=loop)


def consume_and_schedule_compact_self_continuation(
    db: HubDatabase,
    *,
    pending_session_id: str | None,
    target_session: Any,
    loop: Any | None = None,
) -> bool:
    """Consume a fresh marker from a session and schedule its continuation prompt.

    Compaction is an in-place handoff, so the pending marker lives on the same
    session row that emits the provider's readiness event.
    """
    if not pending_session_id:
        return False
    pending = _take_compact_self_continuation_pending(db, pending_session_id)
    if pending is None:
        return False
    source_session_id = pending_session_id
    prompt, payload = pending
    if schedule_compact_self_continuation(target_session, prompt, loop=loop):
        return True
    try:
        _restore_session_variable_if_absent(
            db,
            source_session_id,
            COMPACT_SELF_CONTINUE_VARIABLE,
            payload,
        )
    except Exception:
        logger.warning(
            "Failed to restore compact_self continuation pending for session %s",
            source_session_id,
            exc_info=True,
        )
    return False


async def _send_compact_self_continuation(
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
        ok = await tmux.send_keys(target, f"{prompt}\n", literal=True)
    except Exception:
        logger.warning(
            "Failed to send compact_self continuation prompt for session %s",
            session_id,
            exc_info=True,
        )
        return False
    if not ok:
        logger.warning("tmux send-keys returned false for compact_self continuation %s", session_id)
    return bool(ok)


async def _continue_after_codex_compaction_ready(
    db: HubDatabase,
    *,
    tmux: Any,
    target: str,
    pending_session_id: str,
    before_command: str,
    poll_seconds: float,
    fresh_seconds: int = COMPACT_SELF_CONTINUE_FRESH_SECONDS,
) -> None:
    """Consume and submit only after Codex renders a fresh completion marker."""
    baseline_count = before_command.count(_CODEX_COMPACT_READY_MARKER)
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
        if COMPACT_SELF_CONTINUE_VARIABLE not in variables:
            return

        try:
            output = await tmux.capture_pane(
                target,
                lines=CODEX_COMPACT_READY_CAPTURE_LINES,
            )
        except Exception:
            logger.debug(
                "Failed to inspect Codex compact readiness for session %s",
                pending_session_id,
                exc_info=True,
            )
            output = None

        fresh_output = (
            _fresh_terminal_output(before_command, output) if isinstance(output, str) else ""
        )
        ready = isinstance(output, str) and (
            output.count(_CODEX_COMPACT_READY_MARKER) > baseline_count
            or _CODEX_COMPACT_READY_MARKER in fresh_output
        )
        if ready:
            if poll_seconds > 0:
                await asyncio.sleep(poll_seconds)
            pending = await asyncio.to_thread(
                _take_compact_self_continuation_pending,
                db,
                pending_session_id,
            )
            if pending is None:
                return
            prompt, payload = pending
            sent = await _send_compact_self_continuation(
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
                    COMPACT_SELF_CONTINUE_VARIABLE,
                    payload,
                )
            return

        if poll_seconds > 0:
            await asyncio.sleep(poll_seconds)

    logger.warning(
        "Timed out waiting for Codex compact readiness for session %s",
        pending_session_id,
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


def _schedule_coroutine(coro: Any, *, loop: Any | None = None) -> bool:
    try:
        running_loop = asyncio.get_running_loop()
        # Fire-and-forget: the coroutine logs its own failures and must not block startup.
        task = running_loop.create_task(coro)
        _COMPACT_SELF_CONTINUATION_TASKS.add(task)
        task.add_done_callback(_COMPACT_SELF_CONTINUATION_TASKS.discard)
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


def _collect_compact_resume_required_skills(variables: dict[str, Any]) -> list[str]:
    skills: list[str] = []
    for key in COMPACT_RESUME_SKILL_VARIABLE_KEYS:
        _extend_unique_strings(skills, variables.get(key))
    return _prepare_compact_resume_skills(skills)


def _prepare_compact_resume_skills(values: list[str]) -> list[str]:
    skills = _unique_strings(values)
    if len(skills) <= 1:
        return skills

    return [LOADING_SKILLS_NAME, *(skill for skill in skills if skill != LOADING_SKILLS_NAME)]


def _build_wait_for_summary_directive(summary_session_id: str | None) -> str:
    if summary_session_id:
        return (
            _COMPACT_SELF_CONTINUE_INTRO + "If startup context contains "
            f"`{INJECTED_CONTEXT_BEGIN}`, use that injected context directly and continue. "
            "Only if the injected context is missing or incomplete, call "
            "`gobby-sessions.wait_for_summary("
            f'session_id="{summary_session_id}"'
            ")`. If it returns `completed=false`, repeat the same wait call. "
            "Once complete, use the returned `context` and continue."
        )
    return COMPACT_SELF_CONTINUE_PROMPT


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


def _pop_session_variable(db: HubDatabase, session_id: str, name: str) -> Any:
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
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
