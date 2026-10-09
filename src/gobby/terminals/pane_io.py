"""Backend-neutral pane input/output for daemon-driven CLI injections.

``PaneIO`` is the seam the handoff senders write against: a live terminals row
routes through the registered ``TerminalRuntime`` (tmux or native gterm), and a
session that only carries a tmux pane in its terminal context falls back to the
raw tmux manager.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID
from weakref import WeakKeyDictionary

from gobby.agents.detection.provider import DetectionRegistry
from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, ComposerRead, IdleDetector, plain_text
from gobby.terminals.composer_ledger import (
    composer_drain_keys,
    read_composer,
    record_composer_drain,
)
from gobby.terminals.key_bytes import tmux_key_name
from gobby.terminals.native_runtime import (
    NativeBatchFailure,
    NativeBatchOperation,
    NativeBatchTarget,
    NativeTerminalRuntime,
)
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    NamedKey,
    SnapshotMode,
    TerminalRuntime,
    TerminalWriteError,
)
from gobby.terminals.write_coordinator import (
    IdempotencyConflictError,
    WriteCoordinator,
    WriteRequest,
)

if TYPE_CHECKING:
    from gobby.storage.terminals import Terminal

__all__ = [
    "COMPOSER_MATCH_CHARS",
    "COMPOSER_NOT_CLEAN_ERROR_CODE",
    "DEFAULT_SNAPSHOT_LINES",
    "SUBMIT_ENTER_GAP_SECONDS",
    "SUBMIT_HELD_RETRY_SECONDS",
    "SUBMIT_UNVERIFIED_ERROR_CODE",
    "SUBMIT_VERIFY_SECONDS",
    "TEXT_NOT_SUBMITTED_ERROR_CODE",
    "ComposerReader",
    "ComposerVerdict",
    "CoordinatorPaneIO",
    "DrainResult",
    "PaneIO",
    "RuntimePaneIO",
    "SendResult",
    "SubmitResult",
    "TmuxPaneIO",
    "clear_composer",
    "composer_reader",
    "composer_verdict",
    "context_runtime_pane",
    "live_runtime_pane",
    "log_pane_failure",
    "send_pane_key",
    "submit_coordinated_text",
    "submit_text",
]

logger = logging.getLogger(__name__)

# tmux key names for the NamedKeys the senders use; the runtime path encodes
# every NamedKey itself.
SendResult = tuple[bool, str | None]
# Lines a snapshot returns by default; senders compare pane output around a
# submitted command with it, never the composer itself.
DEFAULT_SNAPSHOT_LINES = 80

#: Classifies a composer frame for a provider (``IdleDetector.composer_read``).
ComposerReader = Callable[[str | None], ComposerRead]
#: What the composer says about a text we just pressed Enter on. ``left`` and
#: ``held`` are positive reads; ``unreadable`` is no evidence in either direction,
#: so it can neither prove a submission nor condemn one.
ComposerVerdict = Literal["left", "held", "changed", "unreadable"]
#: Leading characters that identify our own text on the composer's first row.
#: A wrapped draft only shows its first row, so the whole text never matches.
COMPOSER_MATCH_CHARS = 24
#: How long a submitted text is given to leave the composer before the next rung.
SUBMIT_VERIFY_SECONDS = 2.0
SUBMIT_HELD_RETRY_SECONDS = 30.0
_SUBMIT_VERIFY_POLL_SECONDS = 0.1
#: Gap held between the write and its Enter. Claude Code folds a newline into any
#: single stdin read of 64 bytes or more and inserts the whole run literally (read
#: from the 2.1.278 bundle), so a long text submits only when a Return arrives in a
#: read of its own. 1.5s is the delay that submitted live pull prompts for months
#: before gobby#22550, as HANDOFF_COMPACT_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS; the
#: CLI's own pty driver waits 10ms, so this is margin, not a measured minimum.
SUBMIT_ENTER_GAP_SECONDS = 1.5
COMPOSER_NOT_CLEAN_ERROR_CODE = "composer_not_clean"
COMPOSER_UNKNOWN_ERROR_CODE = "composer_unknown"
TEXT_NOT_SUBMITTED_ERROR_CODE = "command_not_submitted"
ENTER_DELIVERY_UNCONFIRMED_ERROR_CODE = "enter_delivery_unconfirmed"
#: The write and Enter were delivered, but no read proved the draft left the composer.
#: Callers must not count it as submitted and must not retype it (#23188).
SUBMIT_UNVERIFIED_ERROR_CODE = "submit_unverified"


class PaneIO(Protocol):
    """Minimal pane surface: named keys, literal text, and a bottom snapshot."""

    @property
    def backend(self) -> str: ...

    @property
    def target(self) -> str: ...

    async def send_key(self, key: NamedKey) -> SendResult: ...

    # A trailing newline submits the text, matching tmux's literal send.
    async def type_text(self, text: str) -> SendResult: ...

    async def snapshot(
        self, lines: int = DEFAULT_SNAPSHOT_LINES, *, mode: SnapshotMode = "text"
    ) -> str | None: ...


def composer_reader(registry: DetectionRegistry, cli_source: str | None) -> ComposerReader | None:
    """Bind a provider composer reader when its installed manifest supports one."""
    if not cli_source:
        return None
    detector = IdleDetector(registry, cli_source)
    return detector.composer_read if detector.reads_composer() else None


def live_runtime_pane(
    session_id: str,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> RuntimePaneIO | None:
    """PaneIO over the session's live terminals row, or None when it has none."""
    if terminal_manager is None or terminal_runtime_registry is None:
        return None
    terminal = terminal_manager.get_live_for_session(session_id)
    if terminal is None:
        return None
    return RuntimePaneIO(terminal_runtime_registry.resolve(terminal.backend), terminal)


def context_runtime_pane(
    session: Any,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> RuntimePaneIO | None:
    """Live gterm row named by terminal context when the session row is unbound."""
    if terminal_manager is None or terminal_runtime_registry is None:
        return None
    context = getattr(session, "terminal_context", None)
    if not isinstance(context, dict):
        return None
    raw_id = context.get("gobby_terminal_id")
    if not isinstance(raw_id, str) or not raw_id:
        return None
    try:
        terminal_id = str(UUID(raw_id))
    except ValueError:
        return None
    getter = getattr(terminal_manager, "get", None)
    if not callable(getter):
        return None
    terminal = getter(terminal_id)
    if terminal is None or getattr(terminal, "state", None) not in {"pending", "live"}:
        return None
    if getattr(terminal, "project_id", None) != getattr(session, "project_id", None):
        return None
    if getattr(terminal, "agent_run_id", None) is not None:
        return None
    bound = getattr(terminal, "session_id", None)
    if bound not in {None, getattr(session, "id", None)}:
        return None
    return RuntimePaneIO(terminal_runtime_registry.resolve(terminal.backend), terminal)


class RuntimePaneIO:
    """PaneIO over a live terminals row and its registered runtime."""

    def __init__(self, runtime: TerminalRuntime, terminal: Any) -> None:
        self._runtime = runtime
        self._terminal = terminal

    @property
    def backend(self) -> str:
        return str(getattr(self._terminal, "backend", "runtime"))

    @property
    def target(self) -> str:
        return str(getattr(self._terminal, "id", ""))

    async def send_key(self, key: NamedKey) -> SendResult:
        try:
            if isinstance(self._runtime, NativeTerminalRuntime):
                return await self._send_native_input(
                    self._runtime, NativeBatchOperation("key", key)
                )
            outcome = await self._runtime.write_key(self._terminal, key)
        except IndeterminateWrite as exc:
            return _outcome_result(exc, f"{self.backend} key write")
        except TerminalWriteError as exc:
            return False, f"{self.backend} key write failed ({exc.stage}): {key}"
        return _outcome_result(outcome, f"{self.backend} key write")

    async def type_text(self, text: str) -> SendResult:
        if isinstance(self._runtime, NativeTerminalRuntime):
            return await self._send_native_input(self._runtime, NativeBatchOperation("text", text))
        body = text.rstrip("\n")
        try:
            outcome = await self._runtime.write_text(self._terminal, body, submit=body != text)
        except IndeterminateWrite as exc:
            return _outcome_result(exc, f"{self.backend} text write")
        except TerminalWriteError as exc:
            return False, f"{self.backend} text write failed ({exc.stage})"
        return _outcome_result(outcome, f"{self.backend} text write")

    async def _send_native_input(
        self, runtime: NativeTerminalRuntime, operation: NativeBatchOperation
    ) -> SendResult:
        # Batch delivery preserves typed refusal details for the shared ladder.
        # The ladder sends Enter separately after text.
        results = await runtime.write_batch(
            [NativeBatchTarget("pane-input", self._terminal, (operation,))]
        )
        outcome = results[0].outcome
        if isinstance(outcome, IndeterminateWrite):
            # The input may have reached Codex despite a lost host reply.
            raise outcome
        if isinstance(outcome, NativeBatchFailure):
            return (
                False,
                f"{self.backend} input was not delivered ({outcome.stage}): {outcome.code}",
            )
        return _outcome_result(outcome, f"{self.backend} input")

    async def snapshot(
        self, lines: int = DEFAULT_SNAPSHOT_LINES, *, mode: SnapshotMode = "text"
    ) -> str | None:
        try:
            result = await self._runtime.snapshot(self._terminal, lines, mode=mode)
        except Exception:
            logger.debug(
                "Failed to snapshot %s terminal %s", self.backend, self.target, exc_info=True
            )
            return None
        return result.text


class CoordinatorPaneIO:
    """Pane adapter whose writes retain coordinator locking and idempotency."""

    def __init__(
        self,
        coordinator: WriteCoordinator,
        runtime: TerminalRuntime,
        terminal: Terminal,
        *,
        action_key: str,
        idempotency_key: str,
        skip_delivered_text: bool = False,
        resume_epoch: int = 0,
    ) -> None:
        self._coordinator = coordinator
        self._runtime_pane = RuntimePaneIO(runtime, terminal)
        self._terminal = terminal
        self._action_key = action_key
        self._idempotency_key = idempotency_key
        self._skip_delivered_text = skip_delivered_text
        self._resume_epoch = resume_epoch
        self._text_accepted = skip_delivered_text
        self._write_count = 0

    @property
    def text_accepted(self) -> bool:
        return self._text_accepted

    @property
    def backend(self) -> str:
        return self._runtime_pane.backend

    @property
    def target(self) -> str:
        return self._runtime_pane.target

    async def send_key(self, key: NamedKey) -> SendResult:
        return await self._dispatch("key", key, submit=False)

    async def type_text(self, text: str) -> SendResult:
        if self._skip_delivered_text:
            self._skip_delivered_text = False
            return True, None
        body = text.rstrip("\n")
        ok, reason = await self._dispatch("text", body, submit=body != text)
        if ok:
            self._text_accepted = True
        return ok, reason

    async def snapshot(
        self, lines: int = DEFAULT_SNAPSHOT_LINES, *, mode: SnapshotMode = "text"
    ) -> str | None:
        return await self._runtime_pane.snapshot(lines, mode=mode)

    async def _dispatch(
        self, kind: Literal["text", "key"], payload: str, *, submit: bool
    ) -> SendResult:
        index = self._write_count
        self._write_count += 1
        if self._resume_epoch:
            # A fresh key: the delivered text latch is already gone, and a latched
            # indeterminate Enter must not swallow this bare-Enter resume.
            action_key = f"{self._action_key}:resume:{self._resume_epoch}:{index}"
            idempotency_key = None
        else:
            action_key = self._action_key if index == 0 else f"{self._action_key}:{index}"
            idempotency_key = self._idempotency_key if index == 0 else None
        outcome = await self._coordinator.write(
            WriteRequest(
                terminal_id=self._terminal.id,
                action_key=action_key,
                origin="daemon",
                kind=kind,
                payload=payload,
                submit=submit,
                idempotency_key=idempotency_key,
            )
        )
        return _outcome_result(outcome, f"{self.backend} {kind} write")


class TmuxPaneIO:
    """PaneIO over a raw tmux pane resolved from a session's terminal context."""

    def __init__(self, tmux: Any, target: str) -> None:
        self._tmux = tmux
        self._target = target

    @property
    def backend(self) -> str:
        return "tmux"

    @property
    def target(self) -> str:
        return self._target

    async def send_key(self, key: NamedKey) -> SendResult:
        name = tmux_key_name(key)
        if name is None:
            return False, f"tmux has no key name for {key}"
        return await self._dispatch(name, literal=False, action=f"sending {key}")

    async def type_text(self, text: str) -> SendResult:
        return await self._dispatch(text, literal=True, action="typing text")

    async def snapshot(
        self, lines: int = DEFAULT_SNAPSHOT_LINES, *, mode: SnapshotMode = "text"
    ) -> str | None:
        try:
            output = await self._tmux.snapshot_lines(self._target, lines=lines, mode=mode)
        except (TimeoutError, OSError, RuntimeError):
            logger.debug("Failed to capture tmux target %s", self._target)
            return None
        return output if isinstance(output, str) else None

    async def _dispatch(self, keys: str, *, literal: bool, action: str) -> SendResult:
        try:
            ok = await self._tmux.dispatch_keys(self._target, keys, literal=literal)
        except TimeoutError:
            return False, f"tmux send-keys timed out while {action} to {self._target}"
        except (OSError, RuntimeError) as exc:
            detail = str(exc) or type(exc).__name__
            return False, f"tmux send-keys failed while {action} to {self._target}: {detail}"
        if not ok:
            return False, f"tmux send-keys returned false while {action} to {self._target}"
        return True, None


def _outcome_result(outcome: object, action: str) -> SendResult:
    if isinstance(outcome, Delivered):
        return True, None
    if isinstance(outcome, IndeterminateWrite):
        return False, f"{action} was indeterminate: {outcome.detail}"
    return False, f"{action} was not delivered: {type(outcome).__name__}"


@dataclass(frozen=True)
class DrainResult:
    """Whether a drain emptied the composer, and the post-drain verdict when one was read."""

    ok: bool
    reason: str | None = None
    verdict: ComposerVerdict | None = None


async def clear_composer(
    pane: PaneIO,
    cli_source: str | None,
    composer_read: ComposerReader | None = None,
    *,
    verify_seconds: float | None = None,
) -> DrainResult:
    """Drain the composer, read it back, and record the drain on the composer ledger.

    No clear key is trusted to empty the line: Codex binds none (C-u on a held
    ``/compact`` opened the slash popup) and one backspace removes one character.
    Daemon text the ledger knows gets one backspace per character before the
    standard pass. Text of unknown length -- after an interrupt, say -- gets the
    standard pass alone. With a ``composer_read`` the frame is polled through
    ``composer_verdict``: a draft that is still there (``held`` or ``changed``)
    fails the drain and keeps the ledger entry, while ``left`` or ``unreadable``
    records the drain, so only a positive draft stops the caller. Without a reader
    (Grok) the drain stays blind and is recorded. A caller that must prove the
    composer empty checks for the ``left`` verdict. The first key that fails to send
    aborts the drain.
    """
    held = read_composer(pane.target)
    for key in composer_drain_keys(pane.target, cli_source):
        ok, reason = await pane.send_key(key)
        if not ok:
            return DrainResult(False, reason)
    verdict: ComposerVerdict | None = None
    if composer_read is not None:
        window = SUBMIT_VERIFY_SECONDS if verify_seconds is None else verify_seconds
        verdict = await composer_verdict(
            pane, held.line or "", composer_read, window_seconds=max(window, 0.0)
        )
        if verdict in {"held", "changed"}:
            return DrainResult(False, "the composer still shows a draft after the drain", verdict)
    record_composer_drain(pane.target)
    return DrainResult(True, verdict=verdict)


async def composer_gate_for_write(
    pane: PaneIO,
    *,
    action: str,
    pending_payload: str | None = None,
) -> tuple[bool, str | None, str]:
    """Admit a write by the composer ledger's provenance, never by reading the screen.

    ``empty`` admits typing. ``held`` is daemon text: an exact ``pending_payload``
    is admitted as ``held`` for a bare Enter, and any other daemon text as ``stale``
    for the caller to drain. A human ``draft`` refuses, and so does ``unknown`` (a
    blocked, untracked or unbound ledger entry), because only a submit or an
    operator release vouches for that composer again. Callers retain their durable
    fallback when a write is refused.
    """
    read = read_composer(pane.target)
    if read.state == "empty":
        return True, None, "empty"
    if read.state == "held":
        if pending_payload is not None and read.holds_payload(pending_payload):
            return True, None, "held"
        return True, None, "stale"
    _log_ledger_refusal(pane, read)
    if read.state == "draft":
        return False, "composer holds an operator draft", "draft"
    return (
        False,
        f"composer could not be confirmed empty before {action}; once the pane shows an "
        "empty composer, an operator can release it with gobby-sessions release_composer",
        "unknown",
    )


def _log_composer_refusal(
    pane: PaneIO, read: ComposerRead, pending_payload: str | None, snapshot: str | None
) -> None:
    draft_length = len(read.line or "")
    matches_pending = pending_payload is not None and read.holds_payload(pending_payload)
    row_widths = tuple(len(row) for row in plain_text(snapshot).splitlines()) if snapshot else ()
    logger.warning(
        "Composer write refused: target=%s classification=%s draft_length=%d "
        "matches_pending_payload=%s snapshot_source=%s snapshot_mode=ansi "
        "snapshot_rows=%d snapshot_row_widths=%s requested_rows=%d",
        pane.target,
        read.state,
        draft_length,
        matches_pending,
        pane.backend,
        len(row_widths),
        row_widths,
        COMPOSER_PROBE_LINES,
        extra={
            "event": "composer_write_refused",
            "composer_state": read.state,
            "draft_length": draft_length,
            "matches_pending_payload": matches_pending,
            "snapshot_source": pane.backend,
            "snapshot_mode": "ansi",
            "snapshot_row_count": len(row_widths),
            "snapshot_row_widths": row_widths,
            "snapshot_requested_rows": COMPOSER_PROBE_LINES,
        },
    )


def _log_ledger_refusal(pane: PaneIO, read: ComposerRead) -> None:
    # A refused read is a human draft or an unknown entry: the ledger holds no text for it.
    logger.warning(
        "Composer write refused: target=%s classification=%s source=ledger",
        pane.target,
        read.state,
        extra={
            "event": "composer_write_refused",
            "composer_state": read.state,
            "snapshot_source": "ledger",
        },
    )


def log_pane_failure(pane: PaneIO, session_id: str, action: str, reason: str | None) -> None:
    logger.warning(
        "Failed %s on %s target %s for session %s: %s",
        action,
        pane.backend,
        pane.target,
        session_id,
        reason,
        extra={
            "event": "terminal_key_delivery_failed",
            "action": action,
            "backend": pane.backend,
            "target": pane.target,
            "session_id": session_id,
        },
    )


async def send_pane_key(
    pane: PaneIO,
    key: NamedKey,
    session_id: str,
    *,
    action: str,
) -> SendResult:
    """Send one named key and keep failures structured for MCP callers."""
    ok, reason = await pane.send_key(key)
    if not ok:
        log_pane_failure(pane, session_id, action, reason)
        return False, f"{reason} (session {session_id} while {action})"
    return True, None


@dataclass(frozen=True)
class SubmitResult:
    """Whether typed text reached the CLI, and why it did not."""

    ok: bool
    reason: str | None = None
    error_code: str | None = None


async def composer_verdict(
    pane: PaneIO,
    held_text: str,
    composer_read: ComposerReader,
    *,
    window_seconds: float,
    poll_seconds: float = _SUBMIT_VERIFY_POLL_SECONDS,
    pending_payload: str | None = None,
) -> ComposerVerdict:
    """Poll the composer for a positive read of whether it still holds ``held_text``.

    Read through the full window: an initially empty frame can be stale before
    the CLI paints a held draft. At the deadline, ``left`` means the composer is
    empty, ``held`` means it still starts with ``held_text``, ``changed`` means
    a different draft remains, and ``unreadable`` means no classifiable final frame.
    A different draft cannot prove submission: a stale pre-write frame may have
    hidden it, or an operator may have typed it after Enter.
    The read trims its line, so the match trims ``held_text`` too.
    """
    prefix = held_text.strip()[:COMPOSER_MATCH_CHARS]
    elapsed = 0.0
    while True:
        read = composer_read(await pane.snapshot(COMPOSER_PROBE_LINES, mode="ansi"))
        if elapsed >= window_seconds:
            if read.state == "empty":
                return "left"
            if read.state == "draft" and pending_payload is not None:
                return "held" if read.holds_payload(pending_payload) else "changed"
            if read.state == "draft" and read.line is not None and not read.line.startswith(prefix):
                return "changed"
            return "held" if read.state == "draft" else "unreadable"
        delay = min(poll_seconds, window_seconds - elapsed)
        await asyncio.sleep(delay)
        elapsed += delay


async def submit_text(
    pane: PaneIO,
    text: str,
    session_id: str,
    *,
    label: str,
    cli_source: str | None,
    composer_read: ComposerReader | None,
    verify_seconds: float = SUBMIT_VERIFY_SECONDS,
    pending_payload: str | None = None,
) -> SubmitResult:
    """Submit ``text`` into the composer, and prove it left or report it.

    The text and its newline go in as one write, and a bare Enter follows as its own
    stdin read after ``SUBMIT_ENTER_GAP_SECONDS``. Both are needed. A short text such
    as ``/compact`` is submitted by the newline in the write, and the Enter is then a
    no-op on the empty composer. A long text -- every pull prompt -- is not: Claude
    Code folds the newline into any read of 64 bytes or more and inserts the run
    literally, so only the Enter submits it. That delayed Enter carried live handoffs
    for months. The 02:32 rewrite of gobby#22550 made it conditional on a composer
    read taken right after the write, and that read is ``empty`` before the CLI has
    rendered the write, so the Enter was skipped and the prompt stranded, logged as
    delivered.

    Only after the Enter is the composer read back, because only then does a read
    mean anything. ``left`` is proof. A draft that still starts with the text after
    the verify window means the CLI has not accepted the Enter yet, so bare Enters
    are re-sent and verified until the draft leaves or
    ``SUBMIT_HELD_RETRY_SECONDS`` is spent. The text is never retyped. A frame the
    manifest cannot classify after an Enter proves nothing either way, and neither
    does a provider without a ``composer_read``: both return
    ``SUBMIT_UNVERIFIED_ERROR_CODE`` without another Enter, so a caller never records
    an unproven submit as delivered and never types the text a second time.

    The composer is read once before the write, because an operator's
    ``gclient send-keys REF TEXT --enter`` reaches a composer nobody drained. A
    draft already there -- a wake that was typed and never submitted -- keeps its
    place at the head of the composer and the text lands behind it, so an ignored
    Enter leaves that draft, not the text, at the head. Held is matched against
    the draft then; matching the text alone read the stuck draft as ``left``
    (#23730).

    Protected callers supply ``pending_payload`` and are gated by the composer
    ledger instead of that read: an exact held copy gets bare Enter without another
    text write, and every other non-empty composer is refused.
    Protected retry verification also requires the entire pending payload to
    match, so appended operator text cannot receive a retry Enter.
    """
    held_text = text
    already_held = False
    if pending_payload is not None:
        before = read_composer(pane.target)
        if before.state != "empty":
            already_held = before.holds_payload(pending_payload)
            if not already_held:
                _log_ledger_refusal(pane, before)
                return SubmitResult(
                    False,
                    "composer does not hold the pending payload",
                    "composer_occupied" if before.state == "draft" else COMPOSER_UNKNOWN_ERROR_CODE,
                )
    elif composer_read is not None:
        before = composer_read(await pane.snapshot(COMPOSER_PROBE_LINES, mode="ansi"))
        if before.state == "draft":
            held_text = before.line or ""
    try:
        ok, reason = (True, None) if already_held else await pane.type_text(f"{text}\n")
    except IndeterminateWrite as exc:
        # A short command's newline may already have started compaction.
        # Preserve its boundary waiter instead of allowing another submission.
        return SubmitResult(False, str(exc), ENTER_DELIVERY_UNCONFIRMED_ERROR_CODE)
    if not ok:
        log_pane_failure(pane, session_id, f"typing {label}", reason)
        return SubmitResult(False, reason)
    await asyncio.sleep(SUBMIT_ENTER_GAP_SECONDS)

    enter_count = 0
    verify_window = max(verify_seconds, 0.0)
    held_seconds = 0.0
    retried_zero_window = False
    while True:
        ok, reason = await send_pane_key(pane, "enter", session_id, action=f"submitting {label}")
        if not ok:
            snapshot = (
                await pane.snapshot(COMPOSER_PROBE_LINES, mode="ansi")
                if composer_read is not None
                else None
            )
            read = composer_read(snapshot) if composer_read is not None else ComposerRead("unknown")
            _log_composer_refusal(pane, read, text, snapshot)
            if read.holds_payload(text):
                held_seconds += verify_window
                if verify_window == 0:
                    if retried_zero_window:
                        break
                    retried_zero_window = True
                elif held_seconds + verify_window > SUBMIT_HELD_RETRY_SECONDS:
                    break
                await asyncio.sleep(verify_window)
                continue
            # The newline in type_text may already have submitted a short command.
            # A failed follow-up Enter does not prove the provider rejected it.
            return SubmitResult(False, reason, ENTER_DELIVERY_UNCONFIRMED_ERROR_CODE)
        enter_count += 1
        if composer_read is None:
            return SubmitResult(
                False,
                f"{label} was typed and Enter sent, but {cli_source or 'this CLI'} "
                "has no composer reader to verify it",
                SUBMIT_UNVERIFIED_ERROR_CODE,
            )
        verdict = await composer_verdict(
            pane,
            held_text,
            composer_read,
            window_seconds=verify_window,
            pending_payload=pending_payload,
        )
        if verdict == "held":
            held_seconds += verify_window
            if verify_window == 0:
                if retried_zero_window:
                    break
                retried_zero_window = True
            elif held_seconds + verify_window > SUBMIT_HELD_RETRY_SECONDS:
                break
            if enter_count == 1:
                logger.debug(
                    "Session %s still held %s in its composer after Enter; re-sending Enter",
                    session_id,
                    label,
                )
            continue
        if verdict == "unreadable":
            logger.debug(
                "Session %s: composer could not be read after submitting %s; "
                "submission is unverified",
                session_id,
                label,
            )
            return SubmitResult(
                False,
                f"{label} was typed and Enter sent, but the composer could not be read "
                "to verify it",
                SUBMIT_UNVERIFIED_ERROR_CODE,
            )
        if verdict == "changed":
            return SubmitResult(
                False,
                f"{label} was typed and Enter sent, but a different draft remains in the "
                "composer; submission cannot be verified",
                SUBMIT_UNVERIFIED_ERROR_CODE,
            )
        logger.debug(
            "Session %s submitted %s after %d Enter(s); the composer left the draft",
            session_id,
            label,
            enter_count,
        )
        return SubmitResult(True)
    return SubmitResult(
        False,
        f"{label} was typed but stayed in the composer: the CLI never submitted it",
        TEXT_NOT_SUBMITTED_ERROR_CODE,
    )


# WorkspaceOps._write mints a fresh key per call, so this map must stay bounded.
_HELD_VERIFIED_SUBMIT_LIMIT = 32


@dataclass
class _VerifiedSubmitProgress:
    """A verified submit that is still held or indeterminate."""

    text_delivered: bool = False
    payload: str | None = None
    resume_epoch: int = 0


_verified_submits: WeakKeyDictionary[
    WriteCoordinator, dict[tuple[str, str], _VerifiedSubmitProgress]
] = WeakKeyDictionary()


def _held_submit(
    coordinator: WriteCoordinator, terminal_id: str, idempotency_key: str
) -> _VerifiedSubmitProgress | None:
    records = _verified_submits.get(coordinator)
    if records is None:
        return None
    return records.get((terminal_id, idempotency_key))


def _remember_held(
    coordinator: WriteCoordinator,
    terminal_id: str,
    idempotency_key: str,
    progress: _VerifiedSubmitProgress,
) -> None:
    records = _verified_submits.get(coordinator)
    if records is None:
        records = {}
        _verified_submits[coordinator] = records
    key = (terminal_id, idempotency_key)
    if key not in records:
        while len(records) >= _HELD_VERIFIED_SUBMIT_LIMIT:
            del records[next(iter(records))]
    records[key] = progress


def _forget_held(coordinator: WriteCoordinator, terminal_id: str, idempotency_key: str) -> None:
    records = _verified_submits.get(coordinator)
    if records is None:
        return
    records.pop((terminal_id, idempotency_key), None)
    if not records:
        _verified_submits.pop(coordinator, None)


async def submit_coordinated_text(
    coordinator: WriteCoordinator,
    runtime: TerminalRuntime,
    terminal: Terminal,
    text: str,
    session_id: str,
    *,
    action_key: str,
    idempotency_key: str,
    label: str,
    cli_source: str | None,
    composer_read: ComposerReader | None,
) -> SubmitResult:
    """Submit through coordinator locking while retaining composer verification.

    A held or indeterminate finish keeps one bounded record so a retry resumes
    with a bare Enter and does not retype. A verified success drops that record.
    """
    progress = _held_submit(coordinator, terminal.id, idempotency_key)
    if progress is not None and progress.payload not in (None, text):
        raise IdempotencyConflictError("idempotency key was already used with a different payload")
    resume_epoch = 0
    if progress is not None and progress.text_delivered:
        progress.resume_epoch += 1
        resume_epoch = progress.resume_epoch
    pane = CoordinatorPaneIO(
        coordinator,
        runtime,
        terminal,
        action_key=action_key,
        idempotency_key=idempotency_key,
        skip_delivered_text=progress is not None and progress.text_delivered,
        resume_epoch=resume_epoch,
    )
    result = await submit_text(
        pane,
        text,
        session_id,
        label=label,
        cli_source=cli_source,
        composer_read=composer_read,
    )
    if result.ok:
        _forget_held(coordinator, terminal.id, idempotency_key)
        return result
    if pane.text_accepted:
        remembered = progress if progress is not None else _VerifiedSubmitProgress()
        remembered.text_delivered = True
        remembered.payload = text
        remembered.resume_epoch = resume_epoch
        _remember_held(coordinator, terminal.id, idempotency_key, remembered)
    return result
