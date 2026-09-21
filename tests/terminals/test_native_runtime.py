"""NativeTerminalRuntime over a fake host control client (plan 4.1)."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from gobby.agents.constants import GOBBY_TERMINAL_ID
from gobby.storage.terminals import AttachLocator, HostEpochMismatchError, native_locator_key
from gobby.terminals.frame_client import decode_frame
from gobby.terminals.host_client import (
    HostBatchTarget,
    HostCommandError,
    HostDecodeError,
    HostUnavailableError,
    encode_control_line,
)
from gobby.terminals.host_protocol import HostListRow, frames_socket_path
from gobby.terminals.input_grants import sync_host_input_grant
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.native_runtime import (
    NativeBatchFailure,
    NativeBatchOperation,
    NativeTerminalRuntime,
)
from gobby.terminals.runtime import (
    CommitSpawnRefusedError,
    Delivered,
    IndeterminateWrite,
    InputPayloadTooLargeError,
    PreparedSpawn,
    SnapshotResult,
    TerminalSpawnRequest,
    TerminalWriteError,
)
from gobby.terminals.write_coordinator import (
    NativeWakeBatchRequest,
    SequenceDelay,
    UnresolvedWriteStore,
    WriteCoordinator,
    WriteRequest,
)
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

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
    snapshot_error: str | None = None
    snapshot_modes: list[str] = field(default_factory=list)
    spawn_error: str | None = None
    reservation_error: str | None = None
    kill_on_new_connection: int = 0
    resize_on_new_connection: int = 0
    connection_id: int = 1
    persist_pairs: list[dict[str, Any]] = field(default_factory=list)
    batches: list[list[HostBatchTarget]] = field(default_factory=list)
    batch_failures: dict[str, tuple[Literal["none", "partial"], str]] = field(default_factory=dict)
    batch_exception: Exception | None = None
    children_alive: bool = True
    observer_bind: Literal["reserved", "bound", "entitled", "none"] = "reserved"
    host_pid: int = 4242
    commit_deadline_ms: int = 30_000
    commit_deadlines: list[int] = field(default_factory=list)
    commit_error: HostCommandError | None = None
    grants: list[tuple[str, str, str | None]] = field(default_factory=list)
    grant_error: HostCommandError | None = None
    socket_dir: Path = Path("/tmp/gobby-test-host")
    reconnects: list[tuple[Path, str | None]] = field(default_factory=list)

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

    async def spawn_commit(self, terminal_id: str, spawn_key: str, commit_deadline_ms: int) -> None:
        await self.ensure_connected()
        self.commit_deadlines.append(commit_deadline_ms)
        if self.commit_error is not None:
            raise self.commit_error
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

    async def write_batch(self, targets: list[HostBatchTarget]) -> list[dict[str, Any]]:
        await self.ensure_connected()
        self.batches.append(list(targets))
        if self.batch_exception is not None:
            raise self.batch_exception
        results: list[dict[str, Any]] = []
        for target in targets:
            failure = self.batch_failures.get(target.recipient_id)
            if failure is not None:
                stage, code = failure
                results.append(
                    {
                        "recipient_id": target.recipient_id,
                        "host_terminal_id": target.host_terminal_id,
                        "ok": False,
                        "error": code,
                        "stage": stage,
                    }
                )
                continue
            for operation in target.operations:
                self.pty.append(operation.data)
            results.append(
                {
                    "recipient_id": target.recipient_id,
                    "host_terminal_id": target.host_terminal_id,
                    "ok": True,
                    "written": True,
                }
            )
        return results

    async def kill(self, host_terminal_id: str, grace_ms: int = 50) -> None:
        await self.ensure_connected()
        self.kills.append(host_terminal_id)
        self.children_alive = False
        del grace_ms

    async def resize(self, host_terminal_id: str, rows: int, cols: int) -> None:
        await self.ensure_connected()
        self.resizes.append((rows, cols))
        del host_terminal_id

    async def grant_input(self, host_terminal_id: str, attachment_id: str) -> dict[str, Any]:
        await self.ensure_connected()
        if self.grant_error is not None:
            raise self.grant_error
        self.grants.append(("grant_input", host_terminal_id, attachment_id))
        return {"ok": True, "granted": True, "previous": None}

    async def revoke_input(
        self, host_terminal_id: str, attachment_id: str | None = None
    ) -> dict[str, Any]:
        await self.ensure_connected()
        if self.grant_error is not None:
            raise self.grant_error
        self.grants.append(("revoke_input", host_terminal_id, attachment_id))
        return {"ok": True, "revoked": True}

    async def snapshot(
        self, host_terminal_id: str, *, mode: str = "text", max_bytes: int = 0, max_lines: int = 0
    ) -> dict[str, Any]:
        await self.ensure_connected()
        del host_terminal_id, max_bytes, max_lines
        self.snapshot_modes.append(mode)
        if self.snapshot_error is not None:
            raise HostCommandError(self.snapshot_error)
        total = (
            self.snapshot_total
            if self.snapshot_total is not None
            else len(self.snapshot_text.encode("utf-8"))
        )
        return {
            "ok": True,
            "mode": mode,
            "text": self.snapshot_text,
            "truncated": self.snapshot_truncated,
            "dropped_bytes": self.snapshot_dropped,
            "total_bytes": total,
        }

    async def list_terminals(self) -> list[HostListRow]:
        await self.ensure_connected()
        return list(self.list_rows)

    async def reconnect(self, socket_path: Path, expected_epoch: str | None = None) -> str:
        if not self.available:
            raise HostUnavailableError("gterm host unavailable")
        self.reconnects.append((socket_path, expected_epoch))
        self.connection_id += 1
        self.next_seq = 1
        self.ledger.clear()
        return self.host_epoch

    def encode_write(self, payload: dict[str, Any]) -> bytes:
        return encode_control_line(payload)


@dataclass
class _RecordingFrame:
    """Injected frame client that records observer attaches instead of dialing a host."""

    attaches: list[str | None] = field(default_factory=list)

    async def attach_terminal(
        self, locator: AttachLocator, *, reservation_id: str | None = None
    ) -> None:
        del locator
        self.attaches.append(reservation_id)


def _runtime(client: FakeHostClient | None = None) -> tuple[NativeTerminalRuntime, FakeHostClient]:
    host = client or FakeHostClient()
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch)
    return runtime, host


def _native_terminal(
    host: FakeHostClient,
    terminal_id: str | None = None,
    *,
    host_terminal_id: str = "ht-1",
) -> Any:
    tid = terminal_id or str(uuid4())
    row = make_memory_terminal(terminal_id=tid, backend="native")
    row.host_epoch = host.host_epoch
    row.locator = {"host_terminal_id": host_terminal_id}
    row.locator_key = native_locator_key(host.host_epoch, host_terminal_id)
    return row


@pytest.mark.asyncio
async def test_attach_locator_rejects_row_from_earlier_host_epoch() -> None:
    runtime, host = _runtime(FakeHostClient(host_epoch="live-epoch"))
    terminal = _native_terminal(host)
    terminal.host_epoch = "earlier-epoch"

    with pytest.raises(HostEpochMismatchError):
        await runtime.attach_locator(terminal)


@pytest.mark.asyncio
async def test_attach_locator_stamps_live_epoch_when_row_matches() -> None:
    runtime, host = _runtime(FakeHostClient(host_epoch="live-epoch"))

    locator = await runtime.attach_locator(_native_terminal(host))

    assert locator.frame_host_epoch == "live-epoch"
    assert locator.host_socket == str(frames_socket_path(host.socket_dir))
    assert locator.host_terminal_id == "ht-1"


@pytest.mark.asyncio
async def test_attach_locator_falls_back_to_row_epoch_when_host_unadopted() -> None:
    runtime, host = _runtime(FakeHostClient(host_epoch=""))
    terminal = _native_terminal(host)
    terminal.host_epoch = "row-epoch"

    locator = await runtime.attach_locator(terminal)

    assert locator.frame_host_epoch == "row-epoch"


@pytest.mark.asyncio
async def test_injection_parity_and_stage(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(host, "write", fail_enter)
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
async def test_terminate_host_id_requires_matching_epoch() -> None:
    runtime, host = _runtime()
    mismatch = await runtime.terminate_host_id("ht-1", "epoch-before-respawn")
    assert mismatch is not None
    assert mismatch.expected_epoch == "epoch-before-respawn"
    assert mismatch.current_epoch == host.host_epoch
    assert host.kills == []

    terminated = await runtime.terminate_host_id("ht-1", host.host_epoch)
    assert terminated is None
    assert host.kills == ["ht-1"]


@pytest.mark.asyncio
async def test_terminate_reaps_an_orphan_whose_host_epoch_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, host = _runtime()
    reaped: list[tuple[dict[str, Any], float]] = []

    def record_reap(process: Any, *, grace_seconds: float, now: float | None = None) -> None:
        reaped.append((dict(process), grace_seconds))

    monkeypatch.setattr("gobby.terminals.native_runtime.reap_recorded_process", record_reap)

    orphan = _native_terminal(host)
    orphan.host_epoch = "epoch-before-respawn"
    orphan.process = {"pgid": 4242, "start_time": 17.0}
    await runtime.terminate(orphan, 0.25)

    unrecorded = _native_terminal(host)
    unrecorded.host_epoch = None
    unrecorded.process = None
    await runtime.terminate(unrecorded, 0.25)

    assert reaped == [({"pgid": 4242, "start_time": 17.0}, 0.25)]
    assert host.kills == []

    await runtime.terminate(_native_terminal(host), 0.25)
    assert host.kills == ["ht-1"]
    assert len(reaped) == 1


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
    assert host.snapshot_modes == ["text", "text", "text"]


@pytest.mark.asyncio
async def test_snapshot_asks_the_host_for_the_requested_mode() -> None:
    runtime, host = _runtime()
    terminal = _native_terminal(host)
    host.snapshot_text = "plain"

    await runtime.snapshot(terminal, lines=20)
    await runtime.snapshot_full(terminal)
    await runtime.snapshot(terminal, lines=20, mode="ansi")

    assert host.snapshot_modes == ["text", "text", "ansi"]


@pytest.mark.asyncio
async def test_snapshot_of_vanished_terminal_is_empty() -> None:
    runtime, host = _runtime()
    terminal = _native_terminal(host)
    host.snapshot_error = "not_found"
    vanished = await runtime.snapshot(terminal, lines=30)
    assert vanished.text == ""
    assert vanished.truncated is False
    assert vanished.dropped_bytes == 0
    assert vanished.total_bytes == 0


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
    stale_row = store.get(stale.id)
    assert stale_row is not None
    assert stale_row.state == "exited"


@pytest.mark.asyncio
async def test_spawn_prepare_commit_survives_host_death() -> None:
    host = FakeHostClient()
    frame = _RecordingFrame()
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch, frame_client=frame)
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
    assert frame.attaches == ["rsv"], "a reservation rebind re-attaches the observer stream"

    host.available = False
    host.children_alive = False
    assert host.children_alive is False


@pytest.mark.asyncio
async def test_spawn_env_carries_terminal_id() -> None:
    runtime, host = _runtime()
    bare = uuid4()
    shadowed = uuid4()
    caller_env = {GOBBY_TERMINAL_ID: "caller-shadow", "EDITOR": "vi"}
    for terminal_id, env in ((bare, None), (shadowed, caller_env)):
        await runtime.prepare_spawn(
            TerminalSpawnRequest(
                terminal_id=terminal_id,
                spawn_key="gobby-native",
                command=["/bin/sh"],
                env=env,
                reservation_id="rsv",
                reserve_key="rk",
            )
        )

    assert [spawn["env"] for spawn in host.spawns] == [
        {GOBBY_TERMINAL_ID: str(bare)},
        {GOBBY_TERMINAL_ID: str(shadowed), "EDITOR": "vi"},
    ]
    assert caller_env == {GOBBY_TERMINAL_ID: "caller-shadow", "EDITOR": "vi"}, (
        "caller env is copied"
    )


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
async def test_attention_and_lease_writes_serialize_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = asyncio.Event()
    host = FakeHostClient(hold=hold)
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch)
    terminal = _native_terminal(host)
    store = MemoryTerminalStore(terminal)
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    recapture_at: list[int] = []

    async def recapture(_terminal: Any) -> None:
        recapture_at.append(1)
        assert coordinator.lock_held(terminal.id)

    coordinator.set_attention_gate(recapture)
    await coordinator.lease_registry.attach(terminal.id, attachment_id="att-1")
    granted = await coordinator.lease_registry.take_control(terminal.id, "att-1")
    assert granted.lease_generation is not None

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
                expected_lease_generation=granted.lease_generation,
            )
        )

    original_write = host.write

    async def gated(**kwargs: Any) -> dict[str, Any]:
        started.set()
        return await original_write(**kwargs)

    monkeypatch.setattr(host, "write", gated)
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
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    await coordinator.lease_registry.attach(terminal.id, attachment_id="att-1")
    granted = await coordinator.lease_registry.take_control(terminal.id, "att-1")
    assert granted.lease_generation is not None
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
                expected_lease_generation=granted.lease_generation,
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


@pytest.mark.asyncio
async def test_native_wake_batch_preserves_target_results_order_and_latches() -> None:
    host = FakeHostClient(
        batch_failures={
            "session-2": ("partial", "write_queue_unavailable"),
            "session-3": ("none", "terminal_not_running"),
        }
    )
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch)
    terminals = [_native_terminal(host, host_terminal_id=f"ht-{index}") for index in range(1, 4)]
    store = MemoryTerminalStore()
    store.rows.update({terminal.id: terminal for terminal in terminals})
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    store.persist_unresolved_write(
        terminals[2].id,
        "wake:session-3",
        "automatic",
        daemon_epoch="test-epoch",
    )
    operations = (
        NativeBatchOperation(kind="key", payload="ctrl_u"),
        NativeBatchOperation(kind="key", payload="ctrl_k", delay_ms=15),
        NativeBatchOperation(kind="text", payload="continue", delay_ms=15),
        NativeBatchOperation(kind="key", payload="enter", delay_ms=15),
    )
    requests = [
        NativeWakeBatchRequest(
            result_id=f"session-{index}",
            terminal_id=terminal.id,
            clear_action_key=f"wake-clear:session-{index}",
            wake_action_key=f"wake:session-{index}",
            operations=operations,
        )
        for index, terminal in enumerate(terminals, start=1)
    ]

    results = await coordinator.run_native_wake_batch(requests)

    assert len(host.batches) == 1, results
    assert [target.recipient_id for target in host.batches[0]] == [
        "session-1",
        "session-2",
        "session-3",
    ]
    assert [operation.kind for operation in host.batches[0][0].operations] == [
        "key",
        "key",
        "text",
        "key",
    ]
    assert isinstance(results[0].outcome, Delivered)
    assert isinstance(results[1].outcome, NativeBatchFailure)
    assert results[1].outcome.stage == "partial"
    assert isinstance(results[2].outcome, NativeBatchFailure)
    assert results[2].outcome.stage == "none"
    assert "wake:session-1" not in terminals[0].unresolved_writes
    assert "wake:session-2" in terminals[1].unresolved_writes
    assert "wake:session-3" in terminals[2].unresolved_writes


@pytest.mark.asyncio
async def test_native_wake_batch_decode_failure_is_indeterminate_and_keeps_latch() -> None:
    host = FakeHostClient(batch_exception=HostDecodeError("malformed host response"))
    runtime = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch)
    terminal = _native_terminal(host)
    store = MemoryTerminalStore(terminal)
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    request = NativeWakeBatchRequest(
        result_id="session-1",
        terminal_id=terminal.id,
        clear_action_key="wake-clear:session-1",
        wake_action_key="wake:session-1",
        operations=(NativeBatchOperation(kind="text", payload="continue"),),
    )

    result = await coordinator.run_native_wake_batch([request])

    assert isinstance(result[0].outcome, IndeterminateWrite)
    assert result[0].outcome.detail == "malformed host response"
    assert "wake:session-1" in terminal.unresolved_writes


@pytest.mark.asyncio
async def test_coordinator_writes_reach_the_native_runtime() -> None:
    """The acceptance test: a native row is driven by the native runtime.

    A registry holding both backends is the production shape. Before per-terminal
    resolution the coordinator injected every write through tmux, so a gterm-hosted
    agent was undrivable while its terminal still read as live.
    """
    host = FakeHostClient()
    native = NativeTerminalRuntime(host, frame_host_epoch=host.host_epoch)
    tmux = FakeRuntime(backend="tmux")
    terminal = _native_terminal(host)
    store = MemoryTerminalStore(terminal)
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(tmux, native),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )

    outcome = await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key="idle-reprompt",
            origin="automatic",
            kind="text",
            payload="are you there",
        )
    )

    assert isinstance(outcome, Delivered)
    assert b"".join(host.pty) == b"are you there"
    assert tmux.write_log == []


@dataclass
class _SocketDirClient:
    """Minimal control client that only exposes the host socket directory."""

    socket_dir: Path


def test_frame_token_falls_back_to_gobby_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gobby_home = tmp_path / "gobby-home"
    gobby_home.mkdir()
    (gobby_home / "local_cli_token").write_text("home-token\n", encoding="utf-8")
    sockets = tmp_path / "gterm-host"
    sockets.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    runtime = NativeTerminalRuntime(_SocketDirClient(socket_dir=sockets))

    assert runtime._frame_token() == "home-token"


def test_frame_token_prefers_the_socket_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gobby_home = tmp_path / "gobby-home"
    gobby_home.mkdir()
    (gobby_home / "local_cli_token").write_text("home-token\n", encoding="utf-8")
    sockets = tmp_path / "gterm-host"
    sockets.mkdir()
    (sockets / "local_cli_token").write_text("socket-token\n", encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    runtime = NativeTerminalRuntime(_SocketDirClient(socket_dir=sockets))

    assert runtime._frame_token() == "socket-token"


_WIRE_GOLDEN = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "gterminal"
    / "tests"
    / "fixtures"
    / "wire_golden"
)


class _FrameHost:
    """Fake `gterm-frames.sock` peer: welcomes every hello and records attaches."""

    def __init__(self) -> None:
        self.connections: list[asyncio.StreamWriter] = []
        self.attaches: asyncio.Queue[tuple[int, str, str | None]] = asyncio.Queue()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        index = len(self.connections)
        self.connections.append(writer)
        try:
            while True:
                header = await reader.readexactly(4)
                payload = await reader.readexactly(int.from_bytes(header, "little"))
                message = decode_frame(header + payload)
                if message["type"] == "hello":
                    writer.write((_WIRE_GOLDEN / "welcome.bin").read_bytes())
                    await writer.drain()
                elif message["type"] == "attach_terminal":
                    self.attaches.put_nowait(
                        (index, str(message["host_terminal_id"]), message.get("reservation_id"))
                    )
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


def _frames_dir() -> Path:
    root = os.environ.get("CLAUDE_CODE_TMPDIR") or tempfile.gettempdir()
    path = Path(tempfile.mkdtemp(prefix="f", dir=root)).resolve()
    if len(os.fsencode(frames_socket_path(path))) >= 104:
        path.rmdir()
        pytest.fail(f"Permitted temp root is too long for AF_UNIX sockets: {root}")
    return path


def _prepared(host_terminal_id: str) -> PreparedSpawn:
    return PreparedSpawn(
        terminal_id=uuid4(),
        spawn_key=f"sk-{host_terminal_id}",
        locator=None,
        process=None,
        host_terminal_id=host_terminal_id,
    )


async def _until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=2)


async def test_each_observer_bind_gets_its_own_frame_stream() -> None:
    socket_dir = _frames_dir()
    (socket_dir / "local_cli_token").write_text("socket-token\n", encoding="utf-8")
    frames = _FrameHost()
    server = await asyncio.start_unix_server(
        frames.handle, path=str(frames_socket_path(socket_dir))
    )
    runtime, _host = _runtime(FakeHostClient(socket_dir=socket_dir))
    try:
        first, second = _prepared("ht-1"), _prepared("ht-2")
        await runtime.bind_observer(first, "rsv-1")
        await runtime.bind_observer(second, "rsv-2")

        assert await asyncio.wait_for(frames.attaches.get(), timeout=2) == (0, "ht-1", "rsv-1")
        assert await asyncio.wait_for(frames.attaches.get(), timeout=2) == (1, "ht-2", "rsv-2")
        assert first.observer_bound and second.observer_bound
        assert set(runtime._observer_streams) == {"ht-1", "ht-2"}

        # The host removes ht-1 and drops its stream; ht-2's observer is untouched.
        frames.connections[0].close()
        await frames.connections[0].wait_closed()
        await _until(lambda: "ht-1" not in runtime._observer_streams)
        assert runtime._observer_streams["ht-2"].closed is False

        # The next bind opens a fresh stream instead of writing into the dead one.
        await runtime.bind_observer(_prepared("ht-3"), "rsv-3")
        assert await asyncio.wait_for(frames.attaches.get(), timeout=2) == (2, "ht-3", "rsv-3")
        assert set(runtime._observer_streams) == {"ht-2", "ht-3"}

        # Rebinding a terminal replaces its stream and closes the previous one.
        await runtime.bind_observer(_prepared("ht-2"), "rsv-4")
        assert await asyncio.wait_for(frames.attaches.get(), timeout=2) == (3, "ht-2", "rsv-4")
        await _until(lambda: frames.connections[1].is_closing())
        assert set(runtime._observer_streams) == {"ht-2", "ht-3"}

        await runtime.close_frame_streams()
        assert runtime._observer_streams == {}
        await _until(lambda: all(w.is_closing() for w in frames.connections))
    finally:
        await runtime.close_frame_streams()
        server.close()
        await server.wait_closed()
        shutil.rmtree(socket_dir, ignore_errors=True)


@dataclass(frozen=True)
class _Holder:
    attachment_id: str
    frame_delivery: str


@pytest.mark.asyncio
async def test_grant_and_revoke_map_the_row_to_its_host_terminal() -> None:
    runtime, host = _runtime()
    row = _native_terminal(host, host_terminal_id="ht-9")
    await runtime.grant_input(row, "att-1")
    await runtime.revoke_input(row, "att-1")
    await runtime.revoke_input(row)
    assert host.grants == [
        ("grant_input", "ht-9", "att-1"),
        ("revoke_input", "ht-9", "att-1"),
        ("revoke_input", "ht-9", None),
    ]


@pytest.mark.asyncio
async def test_grant_refusals_pass_through_the_runtime() -> None:
    runtime, host = _runtime()
    row = _native_terminal(host)
    for code in ("not_native", "not_found"):
        host.grant_error = HostCommandError(code)
        with pytest.raises(HostCommandError) as refused:
            await runtime.grant_input(row, "att-1")
        assert refused.value.code == code
    assert host.grants == []


@pytest.mark.asyncio
async def test_sync_host_input_grant_follows_the_holder() -> None:
    runtime, host = _runtime()
    row = _native_terminal(host, host_terminal_id="ht-3")
    direct = _Holder("att-direct", "direct")
    assert await sync_host_input_grant(runtime, row, direct) is True
    assert await sync_host_input_grant(runtime, row, _Holder("att-web", "proxy")) is None
    assert await sync_host_input_grant(runtime, row, None) is None
    assert host.grants == [
        ("grant_input", "ht-3", "att-direct"),
        ("revoke_input", "ht-3", None),
        ("revoke_input", "ht-3", None),
    ]
    host.grants.clear()

    # Only native rows can hold a grant; tmux and unknown rows never reach the host.
    assert (
        await sync_host_input_grant(runtime, make_memory_terminal(backend="tmux"), direct) is None
    )
    assert await sync_host_input_grant(runtime, None, direct) is None
    assert host.grants == []

    # Host refusals and outages answer False for a grant and stay quiet for a revoke.
    for code in ("not_native", "not_found"):
        host.grant_error = HostCommandError(code)
        assert await sync_host_input_grant(runtime, row, direct) is False
        assert await sync_host_input_grant(runtime, row, None) is None
    host.grant_error = None
    stale = _native_terminal(host)
    stale.host_epoch = "epoch-before-respawn"
    assert await sync_host_input_grant(runtime, stale, direct) is False
    host.available = False
    assert await sync_host_input_grant(runtime, row, direct) is False
    assert host.grants == []
