"""Composer state per terminal from input provenance, without reading the screen.

Every observation takes the next number on one ledger sequence. A terminal's
composer is clean when nothing human arrived after its clean point: the newest
submit-flagged input that a later provider submit record (``BEFORE_AGENT``, a manual
``PRE_COMPACT``, ``SESSION_START(clear)``) proved consumed. Human input is gterm
``input_activity`` and operator/attention writes; daemon writes are held text the
daemon typed itself. An interrupt key, a host event gap and a provider limit block
until a later submit is recorded or an operator releases the terminal. A terminal
the ledger never tracked reads blocked, so nothing types into a composer whose
history is unknown, until an adopting submit record (for a terminal whose input the
ledger observes) or an operator release starts it clean; a new host epoch drops the
old host's terminals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from gobby.agents.idle_detector import ComposerRead
from gobby.paths import get_gobby_home
from gobby.terminals.composer import COMPOSER_CLEAR_KEYS, composer_clear_sequence
from gobby.terminals.host_events import HostEvent, InputActivityEvent
from gobby.terminals.runtime import NamedKey

if TYPE_CHECKING:
    from gobby.storage.terminals import Terminal

__all__ = [
    "ComposerLedger",
    "ComposerReleaseError",
    "LedgerRead",
    "LedgerState",
    "UnsafeReason",
    "WaitOutcome",
    "WriteOrigin",
    "bind_composer_ledger",
    "composer_drain_keys",
    "composer_ledger_path",
    "load_ledger",
    "persist_ledger",
    "read_composer",
    "record_composer_drain",
    "record_composer_submit",
    "release_composer",
    "write_ledger",
]

logger = logging.getLogger(__name__)

# The daemon's ledger, bound at terminal wiring; composer gates read it.
_bound: ComposerLedger | None = None

LedgerState = Literal["blocked", "draft", "held", "empty"]
UnsafeReason = Literal["interrupt", "gap", "provider_limit"]
WaitOutcome = Literal["resumed", "abandoned", "ambiguous"]
WriteOrigin = Literal["operator", "automatic", "attention", "daemon"]
WriteKind = Literal["text", "key", "paste", "input"]

STATE_VERSION = 1
_HUMAN_ORIGINS = frozenset({"operator", "attention"})
_INTERRUPT_KEYS = frozenset({"escape", "ctrl_c"})


@dataclass(frozen=True)
class LedgerRead:
    """``blocked`` and ``draft`` refuse typing; ``held`` is daemon text (``pending``,
    ``None`` when unknown); ``empty`` admits typing without a drain."""

    state: LedgerState
    reason: str | None = None
    pending: str | None = None

    def holds(self, payload: str) -> bool:
        """Whether the held daemon text is exactly ``payload``, ignoring whitespace runs."""
        return (
            self.state == "held"
            and self.pending is not None
            and " ".join(self.pending.split()) == " ".join(payload.split())
        )


@dataclass
class _Entry:
    clean_seq: int
    human_seq: int = 0
    unsafe: str | None = None
    unsafe_seq: int = 0
    flagged_seq: int = 0
    unflagged_seq: int = 0
    pending_seq: int = 0
    pending_text: str | None = None
    wait_clean: bool | None = None
    dialog_seq: int = 0
    dialog_interrupt: bool = False


class ComposerLedger:
    """Thread-safe composer ledger; hooks call it from threads, the host reader from the loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._entries: dict[str, _Entry] = {}
        self._host: tuple[str, int] | None = None
        self.version = 0

    @property
    def host_cursor(self) -> tuple[str, int] | None:
        return self._host

    @property
    def has_open_waits(self) -> bool:
        with self._lock:
            return any(entry.wait_clean is not None for entry in self._entries.values())

    def read(self, terminal_id: str) -> LedgerRead:
        with self._lock:
            entry = self._entries.get(terminal_id)
            return LedgerRead("blocked", "untracked") if entry is None else _read(entry)

    def release(self, terminal_id: str) -> None:
        """Start ``terminal_id`` clean now: an operator vouching for its composer."""
        with self._lock:
            self._entries[terminal_id] = _Entry(clean_seq=self._next())

    def record_spawn(self, terminal_id: str, epoch: str) -> None:
        """A terminal committed on host ``epoch`` starts with an empty composer.

        A spawn can commit on a new host before the event reader resubscribes. The ledger
        moves to that host from before its first event, so the reader replays it from the
        start; the old host's terminals died with it. An empty ``epoch`` moves nothing.
        """
        with self._lock:
            if epoch and (self._host is None or self._host[0] != epoch):
                self._entries.clear()
                self._host = (epoch, 0)
            self._entries[terminal_id] = _Entry(clean_seq=self._next())

    def observe_host_event(self, event: HostEvent) -> None:
        """Advance the host cursor; input is human, an exit drops its terminal.

        Another host's event is ignored: the reader resumes on the live host, and a spawn
        may have moved the ledger there while a dead host's stream drained.
        """
        with self._lock:
            if self._host is not None and (
                self._host[0] != event.epoch or event.seq <= self._host[1]
            ):
                return
            self._host = (event.epoch, event.seq)
            self.version += 1
            if not isinstance(event, InputActivityEvent):
                self._entries.pop(event.terminal_id, None)
                return
            entry = self._entries.get(event.terminal_id)
            if entry is not None:
                self._human(entry, submit=event.submit, interrupt=event.interrupt is not None)

    def observe_write(
        self,
        terminal_id: str,
        *,
        origin: WriteOrigin,
        kind: WriteKind,
        payload: str,
        submit: bool = False,
    ) -> None:
        with self._lock:
            entry = self._entries.get(terminal_id)
            if entry is None:
                return
            if origin in _HUMAN_ORIGINS:
                self._human(
                    entry,
                    submit=submit
                    or (kind == "key" and payload == "enter")
                    or (kind == "input" and "\r" in payload),
                    interrupt=kind == "key" and payload in _INTERRUPT_KEYS,
                )
                return
            seq = self._next()
            if kind == "key":
                if payload == "enter":
                    entry.flagged_seq = seq
                elif payload in COMPOSER_CLEAR_KEYS:
                    # A clear key removes an unknown amount (one Codex backspace is one
                    # character), so only a sized drain's record empties the entry.
                    if entry.pending_seq > entry.clean_seq:
                        entry.pending_text = None
                else:
                    # An interrupt may restore the prompt; another key may edit it.
                    entry.pending_seq, entry.pending_text = seq, None
                return
            prior = entry.pending_text if entry.pending_seq > entry.clean_seq else ""
            known = prior is not None and kind != "input"
            entry.pending_seq = seq
            entry.pending_text = f"{prior}{payload}" if known else None
            if submit or (kind == "input" and "\r" in payload):
                entry.flagged_seq = seq

    def record_drain(self, terminal_id: str) -> None:
        """The caller drained every character of the held daemon text it read."""
        with self._lock:
            entry = self._entries.get(terminal_id)
            if entry is not None:
                entry.pending_seq, entry.pending_text = 0, None

    def record_submit(self, terminal_id: str, *, adopt: bool = False) -> None:
        """A provider recorded a submit: flagged input up to now left the composer.

        The prompt reached the composer, so no dialog held the input before it. With
        ``adopt``, an untracked terminal whose input the ledger observes, such as one
        bound before the ledger existed, starts clean at the submit: the provider just
        consumed its composer.
        """
        with self._lock:
            entry = self._entries.get(terminal_id)
            if entry is None:
                if adopt:
                    self._entries[terminal_id] = _Entry(clean_seq=self._next())
                return
            _close_wait(entry, "resumed")
            seq = self._next()
            if entry.unflagged_seq > entry.clean_seq:
                # A host without the submit flag cannot say which chunk submitted.
                entry.clean_seq = seq
            elif entry.flagged_seq > entry.clean_seq:
                entry.clean_seq = entry.flagged_seq

    def open_wait(self, terminal_id: str) -> None:
        with self._lock:
            entry = self._entries.get(terminal_id)
            if entry is None:
                return
            self.version += 1
            entry.wait_clean = _read(entry).state in {"empty", "held"}
            entry.dialog_seq, entry.dialog_interrupt = 0, False

    def close_wait(self, terminal_id: str, outcome: WaitOutcome) -> None:
        """A resumed wait consumed its dialog input; any other outcome leaves it human."""
        with self._lock:
            entry = self._entries.get(terminal_id)
            if entry is not None and _close_wait(entry, outcome):
                self.version += 1

    def block(self, terminal_id: str, reason: UnsafeReason) -> None:
        with self._lock:
            entry = self._entries.get(terminal_id)
            if entry is not None:
                entry.unsafe, entry.unsafe_seq = reason, self._next()

    def resume_host(self, epoch: str, seq: int, *, since: int | None, gap: bool) -> None:
        """Re-anchor on a host subscription at ``(epoch, seq)`` that replays after ``since``.

        A new epoch is a new host, whose predecessor's terminals died with it. In the
        same epoch, a subscription that replays nothing past the cursor, while the host
        moved past it, may have lost input, so every terminal blocks. Replay from at or
        before the cursor loses nothing: the events up to it are skipped.
        """
        with self._lock:
            cursor = self._host
            if cursor is not None and cursor[0] != epoch:
                self._entries.clear()
            elif (
                cursor is None or gap or (cursor[1] < seq and (since is None or since > cursor[1]))
            ):
                self._mark_all("gap")
            else:
                return
            self._host = (epoch, seq)
            self.version += 1

    def to_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": STATE_VERSION,
                "seq": self._seq,
                "host": list(self._host) if self._host is not None else None,
                "terminals": {key: asdict(entry) for key, entry in self._entries.items()},
            }

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> ComposerLedger:
        if state.get("version") != STATE_VERSION:
            raise ValueError(f"unsupported composer ledger version: {state.get('version')!r}")
        ledger = cls()
        ledger._seq = int(state["seq"])
        host = state["host"]
        ledger._host = None if host is None else (str(host[0]), int(host[1]))
        ledger._entries = {str(key): _Entry(**value) for key, value in state["terminals"].items()}
        return ledger

    def _next(self) -> int:
        self._seq += 1
        self.version += 1
        return self._seq

    def _human(self, entry: _Entry, *, submit: bool | None, interrupt: bool) -> None:
        seq = self._next()
        if entry.wait_clean:
            entry.dialog_seq = seq
            entry.dialog_interrupt = entry.dialog_interrupt or interrupt
            return
        entry.human_seq = seq
        if interrupt:
            entry.unsafe, entry.unsafe_seq = "interrupt", seq
        if submit is None:
            entry.unflagged_seq = seq
        elif submit:
            entry.flagged_seq = seq

    def _mark_all(self, reason: str) -> None:
        seq = self._next()
        for entry in self._entries.values():
            entry.unsafe, entry.unsafe_seq = reason, seq


def _close_wait(entry: _Entry, outcome: WaitOutcome) -> bool:
    """Close ``entry``'s open wait; whether one was open."""
    if entry.wait_clean is None:
        return False
    if entry.dialog_seq and outcome != "resumed":
        entry.human_seq = max(entry.human_seq, entry.dialog_seq)
        if entry.dialog_interrupt:
            entry.unsafe, entry.unsafe_seq = "interrupt", entry.dialog_seq
    entry.wait_clean, entry.dialog_seq, entry.dialog_interrupt = None, 0, False
    return True


def _read(entry: _Entry) -> LedgerRead:
    if entry.unsafe is not None and entry.unsafe_seq > entry.clean_seq:
        return LedgerRead("blocked", entry.unsafe)
    if entry.human_seq > entry.clean_seq:
        return LedgerRead("draft")
    if entry.pending_seq > entry.clean_seq:
        return LedgerRead("held", pending=entry.pending_text)
    return LedgerRead("empty")


def bind_composer_ledger(ledger: ComposerLedger | None) -> None:
    """Bind the daemon's ledger for composer gates; ``None`` unbinds it."""
    global _bound
    _bound = ledger


def read_composer(terminal_id: str) -> ComposerRead:
    """The bound ledger's composer read; blocked, untracked and unbound read ``unknown``.

    A blocked or untracked entry carries its block reason; an unbound ledger has none.
    """
    if _bound is None:
        return ComposerRead("unknown")
    read = _bound.read(terminal_id)
    if read.state == "blocked":
        return ComposerRead("unknown", reason=read.reason)
    return ComposerRead(read.state, read.pending)


def composer_drain_keys(terminal_id: str, cli_source: str | None) -> tuple[NamedKey, ...]:
    """The standard drain pass, led by one backspace per character of held daemon text.

    Codex binds no line-clear key and one backspace removes one character, so only a
    drain sized to the text the ledger holds can empty it. Read it before sending:
    the first backspace makes the held text unknown.
    """
    read = read_composer(terminal_id)
    backspace: NamedKey = "backspace"
    sized = (backspace,) * len(read.line) if read.state == "held" and read.line else ()
    return (*sized, *composer_clear_sequence(cli_source))


def record_composer_drain(terminal_id: str) -> None:
    """Record on the bound ledger that a drain sized to the held text emptied it."""
    if _bound is not None:
        _bound.record_drain(terminal_id)


def record_composer_submit(terminal_id: str) -> None:
    """Record on the bound ledger that the provider consumed the flagged input."""
    if _bound is not None:
        _bound.record_submit(terminal_id)


class LiveTerminalLookup(Protocol):
    def get_live_for_session(self, session_id: str) -> Terminal | None: ...


class ComposerReleaseError(Exception):
    """The valve refused; ``code`` is the stable error code callers return."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def release_composer(
    terminals: LiveTerminalLookup, session_id: str, *, caller_session_id: str | None
) -> str:
    """The operator valve: vouch that ``session_id``'s composer is empty and start it clean.

    The target session cannot vouch for itself: its own composer is the one in doubt.
    Returns the released terminal id.
    """
    if caller_session_id == session_id:
        raise ComposerReleaseError("self_release", "A session cannot release its own composer")
    ledger = _bound
    if ledger is None:
        raise ComposerReleaseError("ledger_unavailable", "No composer ledger is running")
    terminal = terminals.get_live_for_session(session_id)
    if terminal is None:
        raise ComposerReleaseError("no_live_terminal", f"Session {session_id} has no live terminal")
    ledger.release(terminal.id)
    logger.info("Released the composer of terminal %s (session %s)", terminal.id, session_id)
    return terminal.id


def composer_ledger_path() -> Path:
    return get_gobby_home() / "runtime" / "composer_ledger.json"


def write_ledger(ledger: ComposerLedger, path: Path) -> None:
    """Replace ``path`` atomically with an owner-only snapshot of ``ledger``."""
    data = json.dumps(ledger.to_state(), separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def load_ledger(path: Path) -> ComposerLedger:
    """Restore the state file; an unreadable one restores nothing, so every seat reads blocked."""
    try:
        return ComposerLedger.from_state(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return ComposerLedger()
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        logger.warning(
            "Composer ledger state %s unreadable; every seat starts blocked: %s", path, exc
        )
        return ComposerLedger()


async def persist_ledger(ledger: ComposerLedger, path: Path, *, interval: float = 1.0) -> None:
    """Rewrite the state file at most once per ``interval`` while the ledger changes."""
    saved = ledger.version
    try:
        while True:
            await asyncio.sleep(interval)
            version = ledger.version
            if version != saved:
                await asyncio.to_thread(write_ledger, ledger, path)
                saved = version
    finally:
        if ledger.version != saved:
            write_ledger(ledger, path)
