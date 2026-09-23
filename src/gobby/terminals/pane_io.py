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
from typing import Any, Literal, Protocol

from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, ComposerRead
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.key_bytes import tmux_key_name
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    NamedKey,
    SnapshotMode,
    TerminalRuntime,
    TerminalWriteError,
)

__all__ = [
    "COMPOSER_MATCH_CHARS",
    "COMPOSER_NOT_CLEAN_ERROR_CODE",
    "DEFAULT_SNAPSHOT_LINES",
    "SUBMIT_ENTER_GAP_SECONDS",
    "SUBMIT_HELD_RETRY_SECONDS",
    "SUBMIT_VERIFY_SECONDS",
    "TEXT_NOT_SUBMITTED_ERROR_CODE",
    "ComposerReader",
    "ComposerVerdict",
    "PaneIO",
    "RuntimePaneIO",
    "SendResult",
    "SubmitResult",
    "TmuxPaneIO",
    "clear_composer",
    "composer_verdict",
    "live_runtime_pane",
    "log_pane_failure",
    "send_pane_key",
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
ComposerVerdict = Literal["left", "held", "unreadable"]
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
TEXT_NOT_SUBMITTED_ERROR_CODE = "command_not_submitted"


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
            outcome = await self._runtime.write_key(self._terminal, key)
        except IndeterminateWrite as exc:
            return _outcome_result(exc, f"{self.backend} key write")
        except TerminalWriteError as exc:
            return False, f"{self.backend} key write failed ({exc.stage}): {key}"
        return _outcome_result(outcome, f"{self.backend} key write")

    async def type_text(self, text: str) -> SendResult:
        body = text.rstrip("\n")
        try:
            outcome = await self._runtime.write_text(self._terminal, body, submit=body != text)
        except IndeterminateWrite as exc:
            return _outcome_result(exc, f"{self.backend} text write")
        except TerminalWriteError as exc:
            return False, f"{self.backend} text write failed ({exc.stage})"
        return _outcome_result(outcome, f"{self.backend} text write")

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


async def clear_composer(pane: PaneIO, cli_source: str | None) -> SendResult:
    """Drain the composer blind; the first key that fails to send aborts the drain."""
    for key in composer_clear_sequence(cli_source):
        ok, reason = await pane.send_key(key)
        if not ok:
            return False, reason
    return True, None


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
    text: str,
    composer_read: ComposerReader,
    *,
    window_seconds: float,
    poll_seconds: float = _SUBMIT_VERIFY_POLL_SECONDS,
) -> ComposerVerdict:
    """Poll the composer for a positive read of whether it still holds ``text``.

    ``left`` is an ``empty`` composer or a draft that is no longer ours: the Enter
    landed. ``held`` is a draft whose row still starts with the text after the whole
    window: the CLI never took it. ``unknown`` is what an Enter leaves behind while
    the CLI repaints — no frame, no snapshot, a redraw the manifest cannot classify
    — so it keeps polling, and an ``unknown`` that outlives the window is
    ``unreadable``: no evidence either way, never proof that the text went in.
    """
    prefix = text[:COMPOSER_MATCH_CHARS]
    elapsed = 0.0
    while True:
        read = composer_read(await pane.snapshot(COMPOSER_PROBE_LINES, mode="ansi"))
        if read.state == "empty" or (read.state == "draft" and not read.line.startswith(prefix)):
            return "left"
        if elapsed >= window_seconds:
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
) -> SubmitResult:
    """Submit ``text`` into the drained composer, and prove it left or report it.

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
    manifest cannot classify after an Enter is not evidence of a failure: the write
    and key were both delivered, so the text is reported submitted with a warning.
    Without a ``composer_read`` the delivered write and key are all there is.
    """
    ok, reason = await pane.type_text(f"{text}\n")
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
            return SubmitResult(False, reason)
        enter_count += 1
        if composer_read is None:
            return SubmitResult(True)
        verdict = await composer_verdict(
            pane,
            text,
            composer_read,
            window_seconds=verify_window,
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
                "trusting the delivered write and Enter",
                session_id,
                label,
            )
        else:
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
