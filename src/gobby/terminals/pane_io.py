"""Backend-neutral pane input/output for daemon-driven CLI injections.

``PaneIO`` is the seam the handoff senders write against: a live terminals row
routes through the registered ``TerminalRuntime`` (tmux or native gterm), and a
session that only carries a tmux pane in its terminal context falls back to the
raw tmux manager.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from gobby.terminals.composer import (
    COMPOSER_CAPTURE_LINES,
    COMPOSER_DRAIN_MAX_ROUNDS,
    composer_clear_sequence,
    composer_is_bare,
)
from gobby.terminals.key_bytes import tmux_key_name
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    NamedKey,
    TerminalRuntime,
    TerminalWriteError,
)

__all__ = [
    "PaneIO",
    "RuntimePaneIO",
    "SendResult",
    "TmuxPaneIO",
    "clear_composer",
]

logger = logging.getLogger(__name__)

# tmux key names for the NamedKeys the senders use; the runtime path encodes
# every NamedKey itself.
SendResult = tuple[bool, str | None]


class PaneIO(Protocol):
    """Minimal pane surface: named keys, literal text, and a bottom snapshot."""

    @property
    def backend(self) -> str: ...

    @property
    def target(self) -> str: ...

    async def send_key(self, key: NamedKey) -> SendResult: ...

    async def type_text(self, text: str) -> SendResult: ...

    async def snapshot(self, lines: int = COMPOSER_CAPTURE_LINES) -> str | None: ...


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
        try:
            outcome = await self._runtime.write_text(self._terminal, text, submit=False)
        except IndeterminateWrite as exc:
            return _outcome_result(exc, f"{self.backend} text write")
        except TerminalWriteError as exc:
            return False, f"{self.backend} text write failed ({exc.stage})"
        return _outcome_result(outcome, f"{self.backend} text write")

    async def snapshot(self, lines: int = COMPOSER_CAPTURE_LINES) -> str | None:
        try:
            result = await self._runtime.snapshot(self._terminal, lines)
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

    async def snapshot(self, lines: int = COMPOSER_CAPTURE_LINES) -> str | None:
        try:
            output = await self._tmux.snapshot_lines(self._target, lines=lines)
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


async def clear_composer(
    pane: PaneIO,
    cli_source: str | None,
    *,
    settle_seconds: float = 0.05,
) -> SendResult:
    """Empty the composer and confirm from a snapshot that the prompt line is bare.

    Returns ``(True, None)`` once the prompt line is bare, or once two consecutive
    snapshots are identical (nothing left to drain; a placeholder hint may remain).
    Returns ``(False, reason)`` when a key fails or the drain never settles.
    """
    sequence = composer_clear_sequence(cli_source)
    previous: str | None = None
    for _round in range(COMPOSER_DRAIN_MAX_ROUNDS):
        for key in sequence:
            ok, reason = await pane.send_key(key)
            if not ok:
                return False, reason
        if settle_seconds > 0:
            await asyncio.sleep(settle_seconds)
        capture = await pane.snapshot(COMPOSER_CAPTURE_LINES)
        if capture is None:
            return False, f"{pane.backend} snapshot unavailable while clearing the composer"
        if composer_is_bare(capture) or capture == previous:
            return True, None
        previous = capture
    return False, f"composer on {pane.backend} target {pane.target} did not drain"
