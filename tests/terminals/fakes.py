"""In-memory fakes for TerminalRuntime and write-coordinator tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from gobby.storage.terminals import (
    UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES,
    UNRESOLVED_WRITE_MAX_ENTRIES,
    UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES,
    AttachLocator,
    Terminal,
    UnresolvedWriteCapacityError,
    tmux_locator_key,
)
from gobby.terminals.runtime import (
    Delivered,
    NamedKey,
    PreparedSpawn,
    SnapshotResult,
    TerminalHandle,
    TerminalSpawnRequest,
    WriteOutcome,
)
from gobby.utils.datetime import utc_now

_SOCKET = "/private/tmp/tmux-501/default"


def make_memory_terminal(
    *,
    terminal_id: str | None = None,
    backend: Literal["tmux", "native"] = "tmux",
    unresolved_writes: Mapping[str, Any] | None = None,
    session_name: str | None = None,
) -> Terminal:
    """Build a live Terminal dataclass without touching the database."""
    now = datetime.now(UTC)
    tid = terminal_id or str(uuid4())
    pane_id = "%1"
    locator = {
        "socket_path": _SOCKET,
        "server_pid": 1658,
        "server_start_time": 1784592177,
        "pane_id": pane_id,
    }
    name = session_name or f"gobby-{tid}"
    return Terminal(
        id=tid,
        backend=backend,
        ownership="gobby",
        state="live",
        machine_id=str(uuid4()),
        project_id=str(uuid4()),
        created_at=now,
        updated_at=now,
        attempt_generation=1,
        attempt_started_at=now,
        unresolved_writes=dict(unresolved_writes or {}),
        spawn_key=name,
        locator=locator,
        locator_key=tmux_locator_key(
            socket_path=_SOCKET,
            server_pid=1658,
            server_start_time=1784592177,
            pane_id=pane_id,
        ),
        session_name=name,
        rows=24,
        cols=80,
    )


class MemoryTerminalStore:
    """Latch-aware TerminalManager stand-in for coordinator unit tests."""

    def __init__(self, terminal: Terminal) -> None:
        self.rows: dict[str, Terminal] = {terminal.id: terminal}

    def get(self, terminal_id: str) -> Terminal | None:
        return self.rows.get(terminal_id)

    def persist_unresolved_write(
        self,
        terminal_id: str,
        action_key: str,
        origin: str,
        *,
        at: datetime | None = None,
    ) -> Terminal:
        if (
            not action_key
            or len(action_key.encode("utf-8")) > UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES
        ):
            raise UnresolvedWriteCapacityError()
        current = self.rows[terminal_id]
        writes = dict(current.unresolved_writes)
        if action_key not in writes and len(writes) >= UNRESOLVED_WRITE_MAX_ENTRIES:
            raise UnresolvedWriteCapacityError()
        writes[action_key] = {"at": (at or utc_now()).isoformat(), "origin": origin}
        serialized = json.dumps(writes, separators=(",", ":")).encode("utf-8")
        if len(serialized) > UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES:
            raise UnresolvedWriteCapacityError()
        current.unresolved_writes = writes
        return current

    def clear_unresolved_write(self, terminal_id: str, action_key: str) -> Terminal:
        current = self.rows[terminal_id]
        writes = dict(current.unresolved_writes)
        writes.pop(action_key, None)
        current.unresolved_writes = writes
        return current


@dataclass
class FakeRuntime:
    """Recording TerminalRuntime used by coordinator and registry tests."""

    backend: Literal["tmux", "native"] = "tmux"
    write_log: list[tuple[str, str]] = field(default_factory=list)
    hold: asyncio.Event | None = None
    release: asyncio.Event | None = None
    outcome: WriteOutcome = field(default_factory=Delivered)
    raise_on_write: BaseException | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    create_calls: int = 0
    resize_calls: list[tuple[int, int]] = field(default_factory=list)
    gate: Callable[[], None] | None = None

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        self.create_calls += 1
        return PreparedSpawn(
            terminal_id=request.terminal_id,
            spawn_key=request.spawn_key,
            locator=None,
            process=None,
            host_terminal_id=None,
        )

    async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle:
        if not prepared.persist_acknowledged:
            raise RuntimeError("persist not acknowledged")
        return TerminalHandle(
            terminal_id=prepared.terminal_id,
            locator=AttachLocator(backend=self.backend, frame_host_epoch="epoch"),
        )

    async def is_live(self, terminal: Terminal) -> bool:
        return terminal.state == "live"

    async def snapshot(self, terminal: Terminal, lines: int = 50) -> SnapshotResult:
        return SnapshotResult(text="", truncated=False, dropped_bytes=0, total_bytes=0)

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult:
        return SnapshotResult(text="", truncated=False, dropped_bytes=0, total_bytes=0)

    async def write_text(self, terminal: Terminal, text: str, submit: bool) -> WriteOutcome:
        suffix = "\n" if submit else ""
        return await self._record("text", f"{text}{suffix}")

    async def write_key(self, terminal: Terminal, key: NamedKey) -> WriteOutcome:
        return await self._record("key", key)

    async def write_paste(self, terminal: Terminal, text: str) -> WriteOutcome:
        return await self._record("paste", text)

    async def resize(self, terminal: Terminal, rows: int, cols: int) -> None:
        self.resize_calls.append((rows, cols))

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        return None

    async def attach_locator(self, terminal: Terminal) -> AttachLocator:
        return AttachLocator(backend=self.backend, frame_host_epoch="epoch")

    async def _record(self, kind: str, payload: str) -> WriteOutcome:
        if self.gate is not None:
            self.gate()
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        if self.release is not None:
            self.release.set()
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.write_log.append((kind, payload))
        return self.outcome


def uuid_of(terminal: Terminal) -> UUID:
    return UUID(terminal.id)
