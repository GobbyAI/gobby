"""Composer state per terminal from input provenance, without reading the screen.

Every observation takes the next number on one ledger sequence. A terminal's
composer is clean when nothing human arrived after its clean point: the newest
submit-flagged input that a later provider submit record (``BEFORE_AGENT``, a manual
``PRE_COMPACT``, ``SESSION_START(clear)``) proved consumed. Human input is gterm
``input_activity`` and operator/attention writes; daemon writes are held text the
daemon typed itself. An interrupt key, a host event gap or epoch change, and a
provider limit block until a later submit is recorded or an operator releases the
terminal. A terminal the ledger never tracked reads blocked, so nothing types into
a composer whose history is unknown.
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
from typing import Any, Literal

from gobby.terminals.composer import COMPOSER_CLEAR_KEYS
from gobby.terminals.host_events import InputActivityEvent

__all__ = [
    "ComposerLedger",
    "LedgerRead",
    "LedgerState",
    "UnsafeReason",
    "WaitOutcome",
    "load_ledger",
    "persist_ledger",
    "write_ledger",
]

logger = logging.getLogger(__name__)

LedgerState = Literal["blocked", "draft", "held", "empty"]
UnsafeReason = Literal["interrupt", "gap", "epoch", "provider_limit"]
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

    def read(self, terminal_id: str) -> LedgerRead:
        with self._lock:
            entry = self._entries.get(terminal_id)
            return LedgerRead("blocked", "untracked") if entry is None else _read(entry)

    def release(self, terminal_id: str) -> None:
        """Start ``terminal_id`` clean now: a spawn, or an operator vouching for its composer."""
        with self._lock:
            self._entries[terminal_id] = _Entry(clean_seq=self._next())

    def forget(self, terminal_id: str) -> None:
        with self._lock:
            if self._entries.pop(terminal_id, None) is not None:
                self.version += 1

    def observe_host_input(self, event: InputActivityEvent) -> None:
        with self._lock:
            if self._host is not None and self._host[0] == event.epoch:
                if event.seq <= self._host[1]:
                    return
            elif self._host is not None:
                self._mark_all("epoch")
            self._host = (event.epoch, event.seq)
            self.version += 1
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
                    entry.pending_seq, entry.pending_text = 0, None
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

    def record_submit(self, terminal_id: str) -> None:
        """A provider recorded a submit: flagged input up to now left the composer."""
        with self._lock:
            entry = self._entries.get(terminal_id)
            if entry is None:
                return
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
            if entry is None or entry.wait_clean is None:
                return
            self.version += 1
            if entry.dialog_seq and outcome != "resumed":
                entry.human_seq = max(entry.human_seq, entry.dialog_seq)
                if entry.dialog_interrupt:
                    entry.unsafe, entry.unsafe_seq = "interrupt", entry.dialog_seq
            entry.wait_clean, entry.dialog_seq, entry.dialog_interrupt = None, 0, False

    def block(self, terminal_id: str, reason: UnsafeReason) -> None:
        with self._lock:
            entry = self._entries.get(terminal_id)
            if entry is not None:
                entry.unsafe, entry.unsafe_seq = reason, self._next()

    def mark_host_break(self, reason: Literal["gap", "epoch"], epoch: str, seq: int) -> None:
        """Input may have been missed: block every terminal and resume the host at ``seq``."""
        with self._lock:
            self._mark_all(reason)
            self._host = (epoch, seq)

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


def _read(entry: _Entry) -> LedgerRead:
    if entry.unsafe is not None and entry.unsafe_seq > entry.clean_seq:
        return LedgerRead("blocked", entry.unsafe)
    if entry.human_seq > entry.clean_seq:
        return LedgerRead("draft")
    if entry.pending_seq > entry.clean_seq:
        return LedgerRead("held", pending=entry.pending_text)
    return LedgerRead("empty")


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
