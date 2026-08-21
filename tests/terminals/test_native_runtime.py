"""NativeTerminalRuntime over a fake host control client (plan 4.1)."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from gobby.storage.terminals import native_locator_key
from gobby.terminals.host_client import (
    HostCommandError,
    HostUnavailableError,
    encode_control_line,
)
from gobby.terminals.host_protocol import HostListRow
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.runtime import (
    CommitSpawnRefusedError,
    Delivered,
    IndeterminateWrite,
    InputPayloadTooLargeError,
    SnapshotResult,
    TerminalSpawnRequest,
    TerminalWriteError,
)
from gobby.terminals.write_coordinator import (
    SequenceDelay,
    UnresolvedWriteStore,
    WriteCoordinator,
    WriteRequest,
)
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit

MODE_BRACKETED_PASTE = 2004


@dataclass
class FakeHostClient:
    """Recording control client with a per-connection operation ledger."""

    host_epoch: str = "epoch-1"
    available: bool = True
    closed: bool = False
    bracketed_paste: bool = False
    pty: list[bytes] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    kills: list[str] = field(default_factory=list)
    resizes: list[tuple[int, int]] = field(default_factory=list)
    spawns: list[dict[str, Any]] = field(default_factory=list)
    commits: list[tuple[str, str]] = field(default_factory=list)
    attaches: list[str | None] = field(default_factory=list)
    list_rows: list[HostListRow] = field(default_factory=list)
    ledger: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_seq: int = 1
    drop_next_write: bool = False
    hold: asyncio.Event | None = None
    snapshot_text: str = ""
    snapshot_truncated: bool = False
    snapshot_dropped: int = 0
    snapshot_total: int | None = None
    spawn_error: str | None = None
    reservation_error: str | None = None
    kill_on_new_connection: int = 0
    resize_on_new_connection: int = 0
    connection_id: int = 1
    persist_pairs: list[dict[str, Any]] = field(default_factory=list)
    children_alive: bool = True
    observer_bind: Literal["reserved", "bound", "entitled", "none"] = "reserved"
    host_pid: int = 4242

    async def ensure_connected(self) -> None:
        if not self.available:
            raise HostUnavailableError("gterm host unavailable")

    async def close(self) -> None:
        self.closed = True

    async def spawn(
        self,
        *,
        terminal_id: str,
        spawn_key: str,
        reservation_id: str,
        reserve_key: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        rows: int,
        cols: int,
        commit_deadline_ms: int = 30000,
    ) -> dict[str, Any]:
        await self.ensure_connected()
        if self.reservation_error is not None:
            raise HostCommandError(self.reservation_error)
        if self.spawn_error is not None:
            raise HostCommandError(self.spawn_error)
        request = {
            "method": "spawn",
            "operation_seq": self.next_seq,
            "terminal_id": terminal_id,
            "spawn_key": spawn_key,
            "reservation_id": reservation_id,
            "reserve_key": reserve_key,
            "argv": argv,
            "env": env,
            "cwd": cwd or "/tmp",
            "rows": rows,
            "cols": cols,
            "commit_deadline_ms": commit_deadline_ms,
        }
        self.spawns.append(request)
        self.next_seq += 1
        return {
            "ok": True,
            "method": "spawn_prepared",
            "terminal_id": terminal_id,
            "spawn_key": spawn_key,
            "host_terminal_id": "ht-1",
            "pgid": 99,
            "start_time": 1.0,
            "reservation_id": reservation_id,
            "reserve_key": reserve_key,
            "reserve_generation": 1,
        }

    async def spawn_commit(self, terminal_id: str, spawn_key: str) -> None:
        await self.ensure_connected()
        self.commits.append((terminal_id, spawn_key))

    async def write(
        self,
        *,
        host_terminal_id: str,
        kind: str,
        data: bytes,
        submit: bool = False,
        operation_seq: int | None = None,
    ) -> dict[str, Any]:
        await self.ensure_connected()
        if self.hold is not None:
            await self.hold.wait()
        seq = operation_seq if operation_seq is not None else self.next_seq
        fingerprint = (kind, data, submit, host_terminal_id)
        if seq in self.ledger:
            recorded = self.ledger[seq]
            if recorded["fingerprint"] != fingerprint:
                raise HostCommandError("operation_conflict")
            outcome = recorded["outcome"]
            if not isinstance(outcome, dict):
                raise HostCommandError("operation_conflict")
            return outcome
        if seq < self.next_seq - 1 and seq not in self.ledger:
            raise HostCommandError("operation_expired")
        if seq > self.next_seq:
            raise HostCommandError("operation_gap")
        if self.drop_next_write:
            self.drop_next_write = False
            self.connection_id += 1
            self.next_seq = 1
            self.ledger.clear()
            raise ConnectionError("control dropped")
        payload = {
            "method": "write",
            "operation_seq": seq,
            "host_terminal_id": host_terminal_id,
            "kind": kind,
            "encoding": "utf8-b64",
            "data": base64.b64encode(data).decode("ascii"),
        }
        if kind == "text":
            payload["submit"] = submit
        self.writes.append(payload)
        if kind == "paste" and self.bracketed_paste:
            self.pty.append(b"\x1b[200~" + data + b"\x1b[201~")
        elif kind == "paste":
            self.pty.append(data)
        elif kind == "key":
            name = data.decode("utf-8")
            self.pty.append(b"\n" if name.lower() == "enter" else data)
        else:
            self.pty.append(data + (b"\n" if submit else b""))
        outcome = {"ok": True, "written": True}
        self.ledger[seq] = {"fingerprint": fingerprint, "outcome": outcome}
        self.next_seq = seq + 1
        return outcome

    async def kill(self, host_terminal_id: str, grace_ms: int = 50) -> None:
        await self.ensure_connected()
        self.kills.append(host_terminal_id)
        self.children_alive = False
        del grace_ms

    async def resize(self, host_terminal_id: str, rows: int, cols: int) -> None:
        await self.ensure_connected()
        self.resizes.append((rows, cols))
        del host_terminal_id

    async def snapshot(
        self, host_terminal_id: str, *, mode: str = "text", max_bytes: int = 0, max_lines: int = 0
    ) -> dict[str, Any]:
        await self.ensure_connected()
        del host_terminal_id, mode, max_bytes, max_lines
        total = (
            self.snapshot_total
            if self.snapshot_total is not None
            else len(self.snapshot_text.encode("utf-8"))
        )
        return {
            "ok": True,
            "text": self.snapshot_text,
            "truncated": self.snapshot_truncated,
            "dropped_bytes": self.snapshot_dropped,
            "total_bytes": total,
        }

    async def list_terminals(self) -> list[HostListRow]:
        await self.ensure_connected()
        return list(self.list_rows)

    async def reconnect(self) -> str:
        if not self.available:
            raise HostUnavailableError("gterm host unavailable")
        self.connection_id += 1
        self.next_seq = 1
        self.ledger.clear()
        return self.host_epoch

    def encode_write(self, payload: dict[str, Any]) -> bytes:
        return encode_control_line(payload)


def _runtime(client: FakeHostClient | None = None) -> tuple[NativeTerminalRuntime, FakeHostClient]:
    host = client or FakeHostClient()
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch)
    return runtime, host


def _native_terminal(host: FakeHostClient, terminal_id: str | None = None) -> Any:
    tid = terminal_id or str(uuid4())
    row = make_memory_terminal(terminal_id=tid, backend="native")
    row.host_epoch = host.host_epoch
    row.locator = {"host_terminal_id": "ht-1"}
    row.locator_key = native_locator_key(host.host_epoch, "ht-1")
    return row


@pytest.mark.asyncio
async def test_injection_parity_and_stage() -> None:
    runtime, host = _runtime()
    terminal = _native_terminal(host)
    delivered = await runtime.write_text(terminal, "hello", submit=True)
    assert isinstance(delivered, Delivered)
    assert b"".join(host.pty) == b"hello\n"
    host.pty.clear()
    await runtime.write_text(terminal, "literal", submit=False)
    assert host.pty[0] == b"literal"
    host.pty.clear()
    await runtime.write_key(terminal, "enter")
    assert host.pty[0] and not host.pty[0].endswith(b"\n\n")
    host.pty.clear()
    host.bracketed_paste = True
    await runtime.write_paste(terminal, "line1\nline2")
    assert host.pty[0] == b"\x1b[200~line1\nline2\x1b[201~"
    host.pty.clear()
    host.bracketed_paste = False
    await runtime.write_paste(terminal, "raw\ntext")
    assert host.pty[-1] == b"raw\ntext"

    host.available = False
    with pytest.raises(TerminalWriteError) as none_stage:
        await runtime.write_text(terminal, "nope", submit=False)
    assert none_stage.value.stage == "none"

    runtime, host = _runtime()
    terminal = _native_terminal(host)

    async def fail_enter(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("kind") == "key":
            raise HostCommandError("write_failed")
        return await FakeHostClient.write(host, **kwargs)

    host.write = fail_enter  # type: ignore[method-assign]
    with pytest.raises(TerminalWriteError) as partial:
        await runtime.write_text(terminal, "hello", submit=True)
    assert partial.value.stage == "partial"
    assert host.pty == [b"hello"]


@pytest.mark.asyncio
async def test_no_silent_fallback() -> None:
    runtime, host = _runtime()
    host.available = False
    request = TerminalSpawnRequest(
        terminal_id=uuid4(),
        spawn_key="gobby-native",
        command=["echo", "hi"],
        rows=24,
        cols=80,
        reservation_id="rsv",
        reserve_key="rk",
    )
    with pytest.raises(HostUnavailableError):
        await runtime.prepare_spawn(request)
    assert host.spawns == []


@pytest.mark.asyncio
async def test_write_retry_is_exactly_once() -> None:
    runtime, host = _runtime()
    terminal = _native_terminal(host)
    await runtime.write_text(terminal, "x", submit=True)
    assert b"".join(host.pty) == b"x\n"
    first_seq = host.writes[0]["operation_seq"]
    await runtime.write_text(terminal, "x", submit=False, operation_seq=first_seq)
    assert b"".join(host.pty) == b"x\n"
    with pytest.raises(HostCommandError) as expired:
        await runtime.write_text(terminal, "y", submit=False, operation_seq=0)
    assert expired.value.code == "operation_expired"

    host.drop_next_write = True
    outcome = await runtime.write_text(terminal, "lost", submit=True)
    assert isinstance(outcome, IndeterminateWrite)
    assert b"lost" not in b"".join(host.pty)
    assert not any(item.get("data") == base64.b64encode(b"lost").decode() for item in host.writes)

    await runtime.terminate(terminal, 0.05)
    await runtime.resize(terminal, 30, 100)
    assert host.kills == ["ht-1"]
    assert host.resizes == [(30, 100)]


@pytest.mark.asyncio
async def test_snapshot_metadata_survives_the_adapter() -> None:
    runtime, host = _runtime()
    terminal = _native_terminal(host)
    wide = "盒🙂"
    host.snapshot_text = wide
    visible = await runtime.snapshot(terminal, lines=50)
    assert isinstance(visible, SnapshotResult)
    assert visible.text == wide
    assert visible.truncated is False
    assert visible.dropped_bytes == 0
    assert visible.total_bytes == len(wide.encode("utf-8"))
    full = await runtime.snapshot_full(terminal)
    assert isinstance(full, SnapshotResult)
    host.snapshot_text = "aabb"
    host.snapshot_truncated = True
    host.snapshot_dropped = 12
    host.snapshot_total = 16
    oversized = await runtime.snapshot_full(terminal)
    assert oversized.truncated is True
    assert oversized.dropped_bytes == 12
    assert oversized.total_bytes == 16
    hint = NativeTerminalRuntime.snapshot.__annotations__["return"]
    assert "SnapshotResult" in str(hint)


@pytest.mark.asyncio
async def test_reconnect_reconciles_rows() -> None:
    host = FakeHostClient()
    store = MemoryTerminalStore()
    pending = store.create_pending(
        str(uuid4()),
        str(uuid4()),
        "native",
        "gobby",
        "spawn-a",
        machine_id="machine-1",
    )
    pending.host_epoch = host.host_epoch
    mismatch = store.create_pending(
        str(uuid4()),
        str(uuid4()),
        "native",
        "gobby",
        "spawn-b",
        machine_id="machine-1",
    )
    mismatch.id = pending.id
    store.rows[mismatch.id] = pending
    stale = store.create_pending(
        str(uuid4()),
        str(uuid4()),
        "native",
        "gobby",
        "old-key",
        machine_id="machine-1",
    )
    stale.attempt_started_at = datetime.now(UTC) - timedelta(seconds=120)
    host.list_rows = [
        HostListRow(
            terminal_id=pending.id,
            spawn_key="spawn-a",
            commit_state="committed",
            observer_bind="bound",
            host_terminal_id="ht-1",
            pgid=99,
            start_time=1.0,
        ),
        HostListRow(
            terminal_id=str(uuid4()),
            spawn_key="host-only",
            commit_state="committed",
            observer_bind="none",
            host_terminal_id="ht-orphan",
        ),
    ]
    runtime = NativeTerminalRuntime(
        host,
        frame_host_epoch=host.host_epoch,
        terminal_manager=store,
        machine_id="machine-1",
        spawn_in_doubt_seconds=1.0,
    )
    await runtime.reconnect()
    assert store.get(pending.id) is not None
    assert host.kills == ["ht-orphan"]
    assert store.get(stale.id) is not None
    assert store.get(stale.id).state == "exited"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_spawn_prepare_commit_survives_host_death() -> None:
    runtime, host = _runtime()
    request = TerminalSpawnRequest(
        terminal_id=uuid4(),
        spawn_key="gobby-native",
        command=["/bin/sh"],
        cwd="/tmp",
        rows=24,
        cols=80,
        reservation_id="rsv",
        reserve_key="rk",
    )
    prepared = await runtime.prepare_spawn(request)
    assert prepared.process is not None
    assert prepared.process.pgid == 99
    assert prepared.host_terminal_id == "ht-1"
    with pytest.raises(CommitSpawnRefusedError):
        await runtime.commit_spawn(prepared)
    prepared.acknowledge_persist()
    with pytest.raises(CommitSpawnRefusedError):
        await runtime.commit_spawn(prepared)
    prepared.acknowledge_observer()
    handle = await runtime.commit_spawn(prepared)
    assert handle.locator.host_terminal_id == "ht-1"
    assert handle.locator.frame_host_epoch == host.host_epoch

    host.observer_bind = "none"
    host.list_rows = [
        HostListRow(
            terminal_id=str(request.terminal_id),
            spawn_key=request.spawn_key,
            commit_state="prepared",
            observer_bind="none",
            host_terminal_id="ht-1",
            pgid=99,
            start_time=1.0,
        )
    ]
    with pytest.raises(HostCommandError):
        await runtime.rebind_prepared(prepared)
    host.observer_bind = "reserved"
    host.list_rows[0] = HostListRow(
        terminal_id=str(request.terminal_id),
        spawn_key=request.spawn_key,
        commit_state="prepared",
        observer_bind="reserved",
        host_terminal_id="ht-1",
        pgid=99,
        start_time=1.0,
    )
    await runtime.rebind_prepared(prepared, reservation_id="rsv")
    assert host.attaches[-1] == "rsv"

    host.available = False
    host.children_alive = False
    assert host.children_alive is False


@pytest.mark.asyncio
async def test_control_client_preflights_encoded_line() -> None:
    runtime, host = _runtime()
    terminal = _native_terminal(host)
    oversize = "é" * ((1024 * 1024) + 8)
    with pytest.raises((InputPayloadTooLargeError, HostCommandError)) as exc:
        await runtime.write_paste(terminal, oversize)
    code = getattr(exc.value, "code", None)
    assert code in {None, "request_too_large"}
    assert host.closed is False
    encoded = encode_control_line(
        {
            "method": "write",
            "operation_seq": 4,
            "host_terminal_id": "ht-1",
            "kind": "text",
            "encoding": "utf8-b64",
            "data": "eA==",
            "submit": False,
        }
    )
    assert b"utf8-b64" in encoded
    huge_meta = "n" * (2 * 1024 * 1024)
    with pytest.raises(HostCommandError) as large:
        NativeTerminalRuntime.preflight_line({"method": "spawn", "cwd": huge_meta})
    assert large.value.code == "request_too_large"


@pytest.mark.asyncio
async def test_attention_and_lease_writes_serialize_native() -> None:
    hold = asyncio.Event()
    host = FakeHostClient(hold=hold)
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch)
    terminal = _native_terminal(host)
    store = MemoryTerminalStore(terminal)
    coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime)
    recapture_at: list[int] = []

    async def recapture(_terminal: Any) -> None:
        recapture_at.append(1)
        assert coordinator.lock_held(terminal.id)

    coordinator.set_attention_gate(recapture)
    await coordinator.grant_lease(terminal.id, "att-1")

    async def attention() -> None:
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="attn-1",
                origin="attention",
                kind="text",
                payload="attention",
            )
        )

    started = asyncio.Event()

    async def lease_holder() -> None:
        await started.wait()
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="lease-1",
                origin="operator",
                kind="text",
                payload="lease",
                attachment_id="att-1",
                expected_lease_generation=1,
            )
        )

    original_write = host.write

    async def gated(**kwargs: Any) -> dict[str, Any]:
        started.set()
        return await original_write(**kwargs)

    host.write = gated  # type: ignore[method-assign]
    task_a = asyncio.create_task(attention())
    task_b = asyncio.create_task(lease_holder())
    await started.wait()
    hold.set()
    await asyncio.gather(task_a, task_b)
    payloads = [item["data"] for item in host.writes]
    first = base64.b64decode(payloads[0])
    second = base64.b64decode(payloads[1])
    assert first.startswith(b"attention")
    assert second.startswith(b"lease")
    assert recapture_at == [1]


@pytest.mark.asyncio
async def test_sequence_holds_lock_across_steps_native(monkeypatch: pytest.MonkeyPatch) -> None:
    host = FakeHostClient()
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch)
    terminal = _native_terminal(host)
    store = MemoryTerminalStore(terminal)
    coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime)
    await coordinator.grant_lease(terminal.id, "att-1")
    delay_started = asyncio.Event()
    original_sleep = asyncio.sleep

    async def marked_sleep(delay: float) -> None:
        delay_started.set()
        await original_sleep(delay)

    interleaved: list[str] = []

    async def interloper() -> None:
        await delay_started.wait()
        interleaved.append("trying")
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="lease-text",
                origin="operator",
                kind="text",
                payload="interleave",
                attachment_id="att-1",
                expected_lease_generation=1,
            )
        )
        interleaved.append("done")

    steps: list[WriteRequest | SequenceDelay] = [
        WriteRequest(
            terminal_id=terminal.id,
            action_key="wake",
            origin="automatic",
            kind="key",
            payload="escape",
        ),
        SequenceDelay(0.05),
        WriteRequest(
            terminal_id=terminal.id,
            action_key="wake",
            origin="automatic",
            kind="text",
            payload="hello",
        ),
        SequenceDelay(0.05),
        WriteRequest(
            terminal_id=terminal.id,
            action_key="wake",
            origin="automatic",
            kind="key",
            payload="enter",
        ),
    ]
    task = asyncio.create_task(interloper())
    monkeypatch.setattr("gobby.terminals.write_coordinator.asyncio.sleep", marked_sleep)
    await coordinator.run_sequence(
        terminal.id,
        action_key="wake",
        origin="automatic",
        steps=steps,
    )
    await task
    kinds = [item["kind"] for item in host.writes]
    assert kinds[0] == "key"
    assert "text" in kinds
    assert interleaved == ["trying", "done"]
    assert kinds[-1] == "text" or host.writes[-1]["kind"] in {"text", "key"}
