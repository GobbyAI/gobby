"""In-memory fakes for TerminalRuntime and write-coordinator tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from gobby.storage.terminals import (
    UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES,
    UNRESOLVED_WRITE_MAX_ENTRIES,
    UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES,
    AttachLocator,
    Terminal,
    TerminalManager,
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
_FAKE_SERVER_PID = 4242
_FAKE_PANE_PID = 12345


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
    """Latch-aware TerminalManager stand-in for coordinator and spawn unit tests."""

    def __init__(self, terminal: Terminal | None = None) -> None:
        self.rows: dict[str, Terminal] = {} if terminal is None else {terminal.id: terminal}

    def get(self, terminal_id: str) -> Terminal | None:
        return self.rows.get(terminal_id)

    def get_by_identity(self, terminal_id: str, spawn_key: str) -> Terminal | None:
        current = self.rows.get(terminal_id)
        if current is None or str(current.spawn_key) != spawn_key:
            return None
        return current

    def list_live_by_machine(self, machine_id: str) -> list[Terminal]:
        return [
            row
            for row in self.rows.values()
            if row.machine_id == machine_id and row.state in {"pending", "live"}
        ]

    def mark_exited(self, terminal_id: str) -> Terminal | None:
        current = self.rows.get(terminal_id)
        if current is None:
            return None
        current.state = "exited"
        current.updated_at = datetime.now(UTC)
        return current

    def mark_orphaned(self, terminal_id: str) -> Terminal | None:
        current = self.rows.get(terminal_id)
        if current is None:
            return None
        current.state = "orphaned"
        current.updated_at = datetime.now(UTC)
        return current

    def create_pending(
        self,
        terminal_id: str,
        project_id: str,
        backend: str,
        ownership: str,
        spawn_key: str,
        *,
        machine_id: str | None = None,
        session_id: str | None = None,
        agent_run_id: str | None = None,
        rows: int | None = None,
        cols: int | None = None,
        title: str | None = None,
    ) -> Terminal:
        now = datetime.now(UTC)
        row = Terminal(
            id=terminal_id,
            backend=backend,
            ownership=ownership,
            state="pending",
            machine_id=machine_id or str(uuid4()),
            project_id=project_id,
            created_at=now,
            updated_at=now,
            attempt_generation=1,
            attempt_started_at=now,
            unresolved_writes={},
            spawn_key=spawn_key,
            session_id=session_id,
            agent_run_id=agent_run_id,
            rows=rows,
            cols=cols,
            title=title,
        )
        self.rows[terminal_id] = row
        return row

    def promote_to_live(
        self,
        terminal_id: str,
        *,
        locator: Mapping[str, object],
        locator_key: str,
        host_epoch: str | None = None,
        session_name: str | None = None,
        window_id: str | None = None,
        title: str | None = None,
    ) -> Terminal | None:
        current = self.rows.get(terminal_id)
        if current is None or current.state != "pending":
            return None
        current.state = "live"
        current.locator = dict(locator)
        current.locator_key = locator_key
        current.host_epoch = host_epoch
        if session_name is not None:
            current.session_name = session_name
        if window_id is not None:
            current.window_id = window_id
        if title is not None:
            current.title = title
        current.updated_at = datetime.now(UTC)
        return current

    def fail_pending(self, terminal_id: str) -> Terminal | None:
        current = self.rows.get(terminal_id)
        if current is None or current.state != "pending":
            return None
        current.state = "exited"
        current.updated_at = datetime.now(UTC)
        return current

    def fail_pending_attempt(
        self,
        terminal_id: str,
        *,
        attempt_generation: int,
        attempt_started_at: datetime,
    ) -> Terminal | None:
        current = self.rows.get(terminal_id)
        if (
            current is None
            or current.state != "pending"
            or current.attempt_generation != attempt_generation
            or current.attempt_started_at != attempt_started_at
        ):
            return None
        current.state = "exited"
        current.updated_at = datetime.now(UTC)
        return current

    def bump_attempt_generation(self, terminal_id: str) -> Terminal | None:
        current = self.rows.get(terminal_id)
        if current is None or current.state != "pending":
            return None
        current.attempt_generation += 1
        current.attempt_started_at = datetime.now(UTC)
        current.updated_at = datetime.now(UTC)
        return current

    def record_process(self, terminal_id: str, process: Mapping[str, object]) -> Terminal | None:
        current = self.rows.get(terminal_id)
        if current is None or current.state != "pending":
            return None
        current.process = dict(process)
        return current

    def set_dims(self, terminal_id: str, rows: int, cols: int) -> Terminal | None:
        current = self.rows.get(terminal_id)
        if current is None:
            return None
        current.rows = rows
        current.cols = cols
        current.updated_at = datetime.now(UTC)
        return current

    def get_live_by_session_name(self, session_name: str) -> Terminal | None:
        matches = [
            row
            for row in self.rows.values()
            if row.state in {"pending", "live"}
            and (row.session_name == session_name or row.spawn_key == session_name)
        ]
        return matches[-1] if matches else None

    def get_live_for_session(self, session_id: str) -> Terminal | None:
        matches = [
            row
            for row in self.rows.values()
            if row.session_id == session_id and row.state in {"pending", "live"}
        ]
        return matches[-1] if matches else None

    def list_stale_pending(self, max_age_seconds: float) -> list[Terminal]:
        now = datetime.now(UTC)
        stale: list[Terminal] = []
        for row in self.rows.values():
            if row.state != "pending":
                continue
            age = (now - row.attempt_started_at).total_seconds()
            if age >= max_age_seconds:
                stale.append(row)
        return stale

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

    def clear_all_unresolved_writes(self, terminal_id: str) -> Terminal:
        current = self.rows[terminal_id]
        current.unresolved_writes = {}
        return current

    def set_automatic_write_quarantine(self, terminal_id: str, action_key: str) -> Terminal:
        current = self.rows[terminal_id]
        current.automatic_write_quarantined_at = datetime.now(UTC)
        current.automatic_write_quarantine_action_key = action_key
        current.updated_at = datetime.now(UTC)
        return current

    def clear_automatic_write_quarantine(self, terminal_id: str) -> Terminal:
        current = self.rows[terminal_id]
        current.automatic_write_quarantined_at = None
        current.automatic_write_quarantine_action_key = None
        current.updated_at = datetime.now(UTC)
        return current


@dataclass
class FakeRuntime:
    """Recording TerminalRuntime used by coordinator, registry, and spawn tests."""

    backend: Literal["tmux", "native"] = "tmux"
    write_log: list[tuple[str, str]] = field(default_factory=list)
    hold: asyncio.Event | None = None
    release: asyncio.Event | None = None
    outcome: WriteOutcome = field(default_factory=Delivered)
    outcomes: list[WriteOutcome] = field(default_factory=list)
    snapshot_text: str = ""
    # Per-snapshot texts consumed in order before ``snapshot_text``; an
    # exception entry is raised instead of returning a snapshot.
    snapshot_effects: list[str | BaseException] = field(default_factory=list)
    snapshot_full_result: SnapshotResult | None = None
    raise_on_write: BaseException | None = None
    # When set, ``raise_on_write`` fires only once this many writes were recorded.
    raise_on_write_after: int | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    create_calls: int = 0
    resize_calls: list[tuple[int, int]] = field(default_factory=list)
    gate: Callable[[], None] | None = None
    last_request: TerminalSpawnRequest | None = None
    fail_spawn: bool = False
    typed_fail: bool = False
    spawn_error: str = "spawn failed"
    delay: float = 0.0
    spawn_hold: asyncio.Event | None = None
    live_keys: set[str] = field(default_factory=set)
    killed: list[str] = field(default_factory=list)
    killed_ids: set[str] = field(default_factory=set)
    writes_row: bool = False

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        self.create_calls += 1
        self.last_request = request
        if self.spawn_hold is not None:
            await self.spawn_hold.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.typed_fail:
            from gobby.terminals.runtime import TerminalSpawnFailed

            raise TerminalSpawnFailed(self.spawn_error)
        if self.fail_spawn:
            raise RuntimeError(self.spawn_error)
        self.live_keys.add(request.spawn_key)
        pane_id = "%1"
        stored = {
            "socket_path": _SOCKET,
            "server_pid": _FAKE_SERVER_PID,
            "server_start_time": 1784592177,
            "pane_id": pane_id,
        }
        return PreparedSpawn(
            terminal_id=request.terminal_id,
            spawn_key=request.spawn_key,
            locator=AttachLocator(
                backend=self.backend,
                frame_host_epoch="epoch",
                socket_path=_SOCKET,
                pane_id=pane_id,
            ),
            process=None,
            host_terminal_id=pane_id,
            stored_locator=stored,
            locator_key=tmux_locator_key(
                socket_path=_SOCKET,
                server_pid=_FAKE_SERVER_PID,
                server_start_time=1784592177,
                pane_id=pane_id,
            ),
            pid=_FAKE_PANE_PID,
        )

    async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle:
        if not prepared.persist_acknowledged:
            raise RuntimeError("persist not acknowledged")
        return TerminalHandle(
            terminal_id=prepared.terminal_id,
            locator=AttachLocator(backend=self.backend, frame_host_epoch="epoch"),
        )

    async def is_live(self, terminal: Terminal) -> bool:
        if terminal.id in self.killed_ids:
            return False
        spawn_key = terminal.spawn_key or terminal.session_name
        if spawn_key in self.live_keys:
            return True
        return terminal.state == "live"

    async def snapshot(self, terminal: Terminal, lines: int = 50) -> SnapshotResult:
        del lines
        if self.snapshot_effects:
            effect = self.snapshot_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            return SnapshotResult(
                text=effect,
                truncated=False,
                dropped_bytes=0,
                total_bytes=len(effect.encode("utf-8")),
            )
        if self.snapshot_full_result is not None:
            return SnapshotResult(
                text=self.snapshot_full_result.text,
                truncated=False,
                dropped_bytes=0,
                total_bytes=len(self.snapshot_full_result.text.encode("utf-8")),
            )
        return SnapshotResult(
            text=self.snapshot_text,
            truncated=False,
            dropped_bytes=0,
            total_bytes=len(self.snapshot_text.encode("utf-8")),
        )

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult:
        if self.snapshot_full_result is not None:
            return self.snapshot_full_result
        return SnapshotResult(
            text=self.snapshot_text,
            truncated=False,
            dropped_bytes=0,
            total_bytes=len(self.snapshot_text.encode("utf-8")),
        )

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
        del grace_seconds
        self.killed_ids.add(terminal.id)
        name = terminal.session_name or terminal.spawn_key
        if name is not None:
            self.killed.append(name)
            self.live_keys.discard(name)

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
        if self.raise_on_write is not None and (
            self.raise_on_write_after is None or len(self.write_log) >= self.raise_on_write_after
        ):
            raise self.raise_on_write
        self.write_log.append((kind, payload))
        if self.outcomes:
            return self.outcomes.pop(0)
        return self.outcome


def uuid_of(terminal: Terminal) -> UUID:
    return UUID(terminal.id)


def bind_spawn_runtime(request: object) -> tuple[MemoryTerminalStore, FakeRuntime]:
    """Attach an in-memory manager and FakeRuntime to a SpawnRequest."""
    from gobby.agents.spawn_models import SpawnRequest
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator

    spawn = cast(SpawnRequest, request)

    manager = MemoryTerminalStore()
    runtime = FakeRuntime()
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    spawn.terminal_manager = cast(TerminalManager, manager)
    spawn.terminal_runtime_registry = registry
    spawn.write_coordinator = WriteCoordinator(cast(UnresolvedWriteStore, manager), runtime)
    return manager, runtime
