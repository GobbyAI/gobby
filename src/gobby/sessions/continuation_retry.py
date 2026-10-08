"""Confirm a typed handoff-continuation prompt by the BEFORE_AGENT it triggers.

A composer screen read is not proof of delivery: the 22:28:40 CDT read on
2026-09-21 reported the draft had left while the prompt stayed unsent (a capture
at 22:30 still showed it held, and the turn's BEFORE_AGENT was cancelled). The
only durable proof that the continuation reached the CLI is the BEFORE_AGENT the
session fires for that prompt, recorded as a turn-lifecycle generation bump.
Until it arrives the prompt is re-submitted on a bounded backoff; the bound lets
a session that never fires the hook fall back to durable delivery.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, ComposerRead
from gobby.sessions.turn_lifecycle import TurnLifecycleState
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import LIVE_SESSION_STATUSES, PROTECTED_SESSION_STATUSES, SessionManager
from gobby.terminals.pane_io import (
    ComposerReader,
    PaneIO,
    send_pane_key,
    submit_text,
)

# Per-attempt wait for the session's BEFORE_AGENT and the bounded retry budget.
# Long enough for a loaded daemon to deliver the continuation's own hook, short
# enough that a session which never fires it falls back to durable delivery.
BEFORE_AGENT_CONFIRM_SECONDS = 5.0
BEFORE_AGENT_RETRY_LIMIT = 3
_BEFORE_AGENT_POLL_SECONDS = 0.2

_RESUBMIT_LABEL = "the set_handoff continuation prompt"


def turn_lifecycle_generation(db: HubDatabase, session_id: str) -> int | None:
    """Read the session's turn-lifecycle generation, or None when unreadable.

    A missing attention row is generation 0, not an error: the first BEFORE_AGENT
    creates the row and bumps it, which is what callers watch for.
    """
    try:
        attention = AttentionStateManager(db).get(session_attention_entry_id(session_id))
    except Exception:
        return None
    if attention is None:
        return 0
    return TurnLifecycleState.from_payload(attention.payload).generation


async def await_before_agent(
    db: HubDatabase,
    session_id: str,
    *,
    baseline_generation: int | None,
    timeout_seconds: float = BEFORE_AGENT_CONFIRM_SECONDS,
    poll_seconds: float = _BEFORE_AGENT_POLL_SECONDS,
) -> bool:
    """Poll until the session's turn generation advances past the baseline."""
    baseline = baseline_generation or 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_seconds, 0.0)
    while True:
        generation = await asyncio.to_thread(turn_lifecycle_generation, db, session_id)
        if generation is not None and generation > baseline:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(poll_seconds)


async def _read_composer(pane: PaneIO, composer_read: ComposerReader) -> ComposerRead | None:
    try:
        return composer_read(await pane.snapshot(COMPOSER_PROBE_LINES, mode="ansi"))
    except Exception:
        return None


def continuation_write_allowed(
    db: HubDatabase, session_id: str, *, baseline_generation: int | None = None
) -> bool:
    """Permit the compact's handoff wait, but protect other live interaction waits."""
    return (
        continuation_write_refusal(db, session_id, baseline_generation=baseline_generation) is None
    )


def continuation_write_refusal(
    db: HubDatabase, session_id: str, *, baseline_generation: int | None = None
) -> str | None:
    """Return why a continuation write must not type now, or None when it may."""
    try:
        session = SessionManager(db).get(session_id)
        if session is None or session.status not in LIVE_SESSION_STATUSES:
            return f"session is not live (status={session.status if session else None})"
        if session.status in PROTECTED_SESSION_STATUSES and session.status != "awaiting_handoff":
            return f"session is {session.status}"
        attention = AttentionStateManager(db).get(session_attention_entry_id(session_id))
        state = TurnLifecycleState.from_payload(attention.payload if attention else None)
        if baseline_generation is not None and state.generation > baseline_generation:
            return "a newer turn started"
        waits = sorted({wait.kind for wait in state.waits if wait.kind in {"input", "approval"}})
        return f"session has an open {'/'.join(waits)} wait" if waits else None
    except Exception as exc:
        return f"session state is unreadable ({type(exc).__name__})"


async def resubmit_continuation(
    pane: PaneIO,
    prompt: str,
    session_id: str,
    *,
    cli_source: str | None,
    composer_read: ComposerReader | None,
    verify_seconds: float,
    before_agent_check: Callable[[], Awaitable[bool]] | None = None,
    db: HubDatabase | None = None,
    baseline_generation: int | None = None,
) -> bool:
    """Re-submit the continuation without ever writing over operator text.

    The read that triggered this retry reported the draft left and may have been
    wrong, so a fresh composer read decides, under the same gating every other
    composer writer follows:

    - Our prompt is still held: a bare Enter submits it.
    - An operator draft, or a frame the manifest cannot classify (which may hide
      one): nothing is written. The operator owns that text, so the caller's
      durable fallback delivers the continuation instead.
    - The composer is confirmed empty: a bare Enter goes first, because the read
      may have missed a held copy. The lost prompt is re-pasted only when
      BEFORE_AGENT has still not arrived and a second read still confirms the
      composer empty, so no drain can delete keystrokes and no held copy is
      duplicated.
    - No composer reader: a bare Enter submits a held copy and is a no-op on an
      empty composer; an unverifiable re-paste could only duplicate the prompt.

    Returns False when nothing safe could be written or a pane write failed.
    """

    async def send_enter_if_allowed() -> bool:
        if db is not None and not await asyncio.to_thread(
            continuation_write_allowed, db, session_id, baseline_generation=baseline_generation
        ):
            return False
        return await _send_enter(pane, session_id)

    if composer_read is None:
        return await send_enter_if_allowed()

    read = await _read_composer(pane, composer_read)
    if read is None or read.state == "unknown":
        return False
    if read.state == "draft":
        if not _holds_our_prompt(read, prompt):
            return False
        return await send_enter_if_allowed()

    if not await send_enter_if_allowed():
        return False
    if before_agent_check is not None and await before_agent_check():
        # A held copy the empty read missed was submitted by that Enter and its
        # BEFORE_AGENT arrived; a re-paste would queue the continuation twice.
        return True
    read = await _read_composer(pane, composer_read)
    if read is None or read.state != "empty":
        # A held copy is visible now, or the frame is unreadable or holds operator
        # text: never type over it. The next bounded Enter handles a held copy.
        return read is not None and read.state == "draft" and _holds_our_prompt(read, prompt)
    if db is not None and not await asyncio.to_thread(
        continuation_write_allowed, db, session_id, baseline_generation=baseline_generation
    ):
        return False
    result = await submit_text(
        pane,
        prompt,
        session_id,
        label=_RESUBMIT_LABEL,
        cli_source=cli_source,
        composer_read=composer_read,
        verify_seconds=verify_seconds,
        pending_payload=prompt,
    )
    return result.ok


def _holds_our_prompt(read: ComposerRead, prompt: str) -> bool:
    """True only when the classified draft is exactly our pending prompt."""
    return read.holds_payload(prompt)


async def _send_enter(pane: PaneIO, session_id: str) -> bool:
    """Send a bare Enter that submits a held draft and no-ops on an empty composer."""
    ok, _ = await send_pane_key(
        pane, "enter", session_id, action=f"re-submitting {_RESUBMIT_LABEL}"
    )
    return ok


async def resubmit_until_before_agent(
    pane: PaneIO,
    prompt: str,
    session_id: str,
    *,
    db: HubDatabase,
    baseline_generation: int | None,
    cli_source: str | None,
    composer_read: ComposerReader | None,
    verify_seconds: float,
    retry_limit: int = BEFORE_AGENT_RETRY_LIMIT,
) -> bool:
    """Keep re-submitting until the session's BEFORE_AGENT arrives, then stop.

    A composer read that reports the draft left does not settle this: only the
    BEFORE_AGENT hook the continuation triggers does. Retries are bounded so a
    session that never fires the hook lets the caller fall back to durable
    delivery instead of typing forever.
    """
    for attempt in range(1, retry_limit + 1):
        if await await_before_agent(db, session_id, baseline_generation=baseline_generation):
            return True
        if attempt >= retry_limit:
            break
        await resubmit_continuation(
            pane,
            prompt,
            session_id,
            cli_source=cli_source,
            composer_read=composer_read,
            verify_seconds=verify_seconds,
            db=db,
            baseline_generation=baseline_generation,
            before_agent_check=lambda: await_before_agent(
                db,
                session_id,
                baseline_generation=baseline_generation,
            ),
        )
    return False
