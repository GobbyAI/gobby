"""Async JSON-lines client for gterm-control.sock (plan 4.1)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from gobby.terminals.host_events import HostEventStream, HostInventorySnapshot
from gobby.terminals.host_protocol import (
    CONTROL_PROTOCOL_VERSION,
    HostListRow,
    decode_line,
)
from gobby.terminals.runtime import SnapshotMode

MAX_CONTROL_LINE = 2 * 1024 * 1024
MAX_WRITE_BATCH_TARGETS = 64
MAX_WRITE_BATCH_PAYLOAD_BYTES = 1024 * 1024
MAX_WRITE_BATCH_OPERATIONS_PER_TARGET = 128
MAX_WRITE_BATCH_DELAY_MS = 1_000
MAX_WRITE_BATCH_TOTAL_DELAY_MS = 5_000
# Host returns these before recording operation_seq in the per-connection ledger.
_UNCONSUMED_SEQ_ERRORS = frozenset({"operation_gap", "operation_seq_required", "host_draining"})


class HostEpochChangedError(RuntimeError):
    """Welcome/ping epoch did not match the locator the caller still holds."""


class HostCommandError(RuntimeError):
    """Typed control-protocol refusal (`ok: false`)."""

    def __init__(
        self,
        error: str,
        *,
        code: str | None = None,
        detail: str | None = None,
        stage: str | None = None,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.code = code or error
        self.detail = detail
        self.stage = stage


class HostUnavailableError(HostCommandError):
    """The gterm host cannot be reached: a `host_unavailable` refusal, never a tmux fallback."""

    def __init__(self, message: str = "gterm host unavailable") -> None:
        super().__init__("host_unavailable", detail=message)
        self.message = message


class HostConnectionLost(HostUnavailableError):
    """The active control connection ended while requests were pending."""


class HostNotAdoptedError(HostUnavailableError):
    """The daemon holds no adopted host epoch, so nothing can be verified against one."""

    def __init__(self, message: str = "gterm host not adopted") -> None:
        HostCommandError.__init__(self, "host_not_adopted", detail=message)
        self.message = message


class CommitTransportError(HostUnavailableError):
    """Transport failure while committing, with an explicit write boundary."""

    def __init__(self, message: str, *, request_written: bool) -> None:
        super().__init__(message)
        self.request_written = request_written


class HostManagerStopped(HostUnavailableError):
    """The host manager is stopping, stopped, drained, or exhausted."""


class HostDecodeError(ValueError):
    """Control payload was JSON but missing a required field."""


class ControlSocketWriter(Protocol):
    """Writable half of a Unix-domain control connection."""

    def write(self, data: bytes) -> object: ...

    async def drain(self) -> object: ...

    def close(self) -> object: ...

    async def wait_closed(self) -> object: ...


@dataclass(frozen=True)
class HelloResult:
    host_epoch: str
    version: str
    protocol_version: int


@dataclass(frozen=True)
class PingResult:
    host_epoch: str
    version: str
    host_pid: int


@dataclass(frozen=True)
class HostBatchOperation:
    kind: Literal["text", "key"]
    data: bytes
    delay_ms: int = 0


@dataclass(frozen=True)
class HostBatchTarget:
    recipient_id: str
    host_terminal_id: str
    operations: tuple[HostBatchOperation, ...]


def encode_control_line(payload: dict[str, Any]) -> bytes:
    """Serialize one control request or response as a canonical JSON line."""
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def decode_control_line(raw: bytes | str) -> dict[str, Any]:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return decode_line(text)


class HostClient:
    """Newline-delimited JSON control client with per-connection operation_seq."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: ControlSocketWriter,
        *,
        pid_alive: Callable[[], bool] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._pid_alive = pid_alive
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._pending: dict[str, tuple[int, asyncio.Future[dict[str, Any]]]] = {}
        self._generation = 1
        self._next_request_id = 1
        self._event_queue: asyncio.Queue[dict[str, Any] | BaseException | None] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = asyncio.create_task(
            self._reader_loop(self._generation)
        )
        self.closed = False
        self.host_epoch: str | None = None
        self._control_token: str | None = None
        self._protocol_version = CONTROL_PROTOCOL_VERSION
        self.next_seq = 1
        self._operation_lock = asyncio.Lock()
        self._commit_write_states: dict[asyncio.Task[Any], bool] = {}

    @classmethod
    async def connect(cls, socket_path: Path) -> HostClient:
        try:
            reader, writer = await asyncio.open_unix_connection(
                path=str(socket_path), limit=MAX_CONTROL_LINE + 1
            )
        except (OSError, ConnectionError) as exc:
            raise HostUnavailableError("gterm host unavailable") from exc
        return cls(reader, writer)

    @staticmethod
    def raise_for_payload(payload: dict[str, Any]) -> None:
        if payload.get("ok") is False:
            error = str(payload.get("error", "error"))
            code = payload.get("code")
            detail = payload.get("detail")
            stage = payload.get("stage")
            raise HostCommandError(
                error,
                code=code if isinstance(code, str) else None,
                detail=detail if isinstance(detail, str) else None,
                stage=stage if isinstance(stage, str) else None,
            )

    def _advance_operation_seq(self, request: dict[str, Any], *, error: str | None = None) -> None:
        raw = request.get("operation_seq")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            return
        if error in _UNCONSUMED_SEQ_ERRORS:
            return
        if raw >= self.next_seq:
            self.next_seq = raw + 1

    @staticmethod
    def require_ping(payload: dict[str, Any]) -> dict[str, Any]:
        host_pid = payload.get("host_pid")
        if not isinstance(host_pid, int):
            raise HostDecodeError("host_pid required")
        return payload

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close_generation("control closed")

    async def read_payload(self) -> dict[str, Any]:
        try:
            raw = await self._reader.readline()
        except (asyncio.LimitOverrunError, ValueError) as exc:
            raise HostConnectionLost("control reply exceeds limit") from exc
        if not raw:
            raise HostConnectionLost("control closed")
        if len(raw) > MAX_CONTROL_LINE:
            raise HostConnectionLost("control reply exceeds limit")
        try:
            return decode_control_line(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HostConnectionLost("invalid control reply") from exc

    async def _reader_loop(self, generation: int) -> None:
        try:
            while generation == self._generation:
                payload = await self.read_payload()
                request_id = payload.get("id")
                if request_id is None:
                    self._event_queue.put_nowait(payload)
                    continue
                pending = self._pending.pop(str(request_id), None)
                if pending is None or pending[0] != generation:
                    continue
                future = pending[1]
                if not future.done():
                    future.set_result(payload)
        except asyncio.CancelledError:
            raise
        except (HostConnectionLost, ConnectionError, OSError, TimeoutError) as exc:
            if generation == self._generation:
                self.closed = True
            self._fail_pending(generation, str(exc) or "control connection lost")
            self._event_queue.put_nowait(HostConnectionLost(str(exc) or "control connection lost"))

    def _fail_pending(self, generation: int, message: str) -> None:
        for request_id, (pending_generation, future) in list(self._pending.items()):
            if pending_generation != generation:
                continue
            self._pending.pop(request_id, None)
            if not future.done():
                future.set_exception(HostConnectionLost(message))

    async def _close_generation(self, message: str) -> None:
        generation = self._generation
        self._generation += 1
        self.closed = True
        self._fail_pending(generation, message)
        reader_task = self._reader_task
        self._reader_task = None
        if reader_task is not None and reader_task is not asyncio.current_task():
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (OSError, ConnectionError):
            pass
        self._event_queue.put_nowait(None)

    async def _next_event_payload(self) -> dict[str, Any] | None:
        item = await self._event_queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def _begin_request(self, request: dict[str, Any]) -> asyncio.Future[dict[str, Any]]:
        if self.closed:
            raise HostConnectionLost("control closed")
        request = dict(request)
        request_id = str(request.get("id") or self._next_request_id)
        if "id" not in request:
            self._next_request_id += 1
            request["id"] = request_id
        if request_id in self._pending:
            raise HostCommandError("duplicate_id")
        encoded = encode_control_line(request)
        if len(encoded) >= MAX_CONTROL_LINE:
            raise HostCommandError("request_too_large")
        future = asyncio.get_running_loop().create_future()
        generation = self._generation
        self._pending[request_id] = (generation, future)

        def clear_cancelled(done: asyncio.Future[dict[str, Any]]) -> None:
            current = self._pending.get(request_id)
            if done.cancelled() and current == (generation, done):
                self._pending.pop(request_id, None)

        future.add_done_callback(clear_cancelled)
        try:
            async with self._write_lock:
                self._writer.write(encoded)
                task = asyncio.current_task()
                if task is not None and task in self._commit_write_states:
                    self._commit_write_states[task] = True
                await self._writer.drain()
        except BaseException:
            current = self._pending.get(request_id)
            if current == (generation, future):
                self._pending.pop(request_id, None)
            raise
        return future

    async def _roundtrip(self, request: dict[str, Any]) -> dict[str, Any]:
        is_commit = request.get("method") == "spawn_commit"
        try:
            async with self._lifecycle_lock:
                future = await self._begin_request(request)
            payload = await future
            self.raise_for_payload(payload)
            self._advance_operation_seq(request)
            return payload
        except HostConnectionLost as exc:
            if is_commit:
                task = asyncio.current_task()
                request_written = bool(
                    task is not None and self._commit_write_states.get(task, False)
                )
                raise CommitTransportError(
                    str(exc) or "commit transport failed",
                    request_written=request_written,
                ) from exc
            raise
        except HostCommandError as exc:
            if not isinstance(exc, HostUnavailableError):
                self._advance_operation_seq(request, error=exc.error)
            raise
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, TimeoutError) as exc:
            if is_commit:
                task = asyncio.current_task()
                request_written = bool(
                    task is not None and self._commit_write_states.get(task, False)
                )
                raise CommitTransportError(
                    str(exc) or "commit transport failed",
                    request_written=request_written,
                ) from exc
            raise HostUnavailableError(str(exc) or "gterm host unavailable") from exc

    async def _mutating_roundtrip(
        self, request: dict[str, Any], *, operation_seq: int | None = None
    ) -> dict[str, Any]:
        """Serialize one state-changing request across this connection.

        The host keys its ledger on a per-connection monotonic ``operation_seq``
        and runs every mutating verb through a single ordered dispatcher, so
        pipelining buys no concurrency, while two callers that read ``next_seq``
        before either reply lands take the same sequence and the host refuses the
        loser ``operation_conflict``. Allocation, send, reply, and the advance or
        retain decision therefore all happen under one lock, which is also what
        makes the ``_UNCONSUMED_SEQ_ERRORS`` retain safe: nothing else can have
        taken the sequence being kept.
        """
        async with self._operation_lock:
            request["operation_seq"] = self.next_seq if operation_seq is None else operation_seq
            return await self._roundtrip(request)

    async def hello(self, protocol_version: int, control_token: str) -> HelloResult:
        payload = await self._roundtrip(
            {
                "method": "hello",
                "protocol_version": protocol_version,
                "control_token": control_token,
            }
        )
        self._protocol_version = protocol_version
        self._control_token = control_token
        self.host_epoch = str(payload.get("host_epoch", ""))
        return HelloResult(
            host_epoch=self.host_epoch,
            version=str(payload.get("version", "")),
            protocol_version=int(payload.get("protocol_version", protocol_version)),
        )

    async def ping(self) -> PingResult:
        payload = self.require_ping(await self._roundtrip({"method": "ping"}))
        self.host_epoch = str(payload.get("host_epoch", self.host_epoch or ""))
        return PingResult(
            host_epoch=self.host_epoch,
            version=str(payload.get("version", "")),
            host_pid=int(payload["host_pid"]),
        )

    async def list_terminals(self) -> list[HostListRow]:
        return list((await self.list_inventory()).rows)

    async def list_inventory(self) -> HostInventorySnapshot:
        payload = await self._roundtrip({"method": "list"})
        raw_rows = payload.get("terminals")
        rows = (
            tuple(HostListRow.from_mapping(item) for item in raw_rows if isinstance(item, dict))
            if isinstance(raw_rows, list)
            else ()
        )
        return HostInventorySnapshot(
            rows=rows,
            epoch=str(payload.get("epoch", self.host_epoch or "")),
            seq=int(payload.get("seq", 0)),
        )

    async def host_shutdown(self, grace_ms: int) -> dict[str, bool]:
        try:
            payload = await self._roundtrip(
                {"method": "host_shutdown", "grace_ms": max(0, int(grace_ms))}
            )
        except (ConnectionError, TimeoutError, HostCommandError, OSError):
            if self._pid_alive is not None and not self._pid_alive():
                return {"accepted": True, "draining": True}
            raise
        return {
            "accepted": bool(payload.get("accepted", True)),
            "draining": bool(payload.get("draining", True)),
        }

    async def spawn(self, **fields: Any) -> dict[str, Any]:
        raw_seq = fields.pop("operation_seq", None)
        return await self._mutating_roundtrip(
            {"method": "spawn", **fields},
            operation_seq=None if raw_seq is None else int(raw_seq),
        )

    async def spawn_commit(self, terminal_id: str, spawn_key: str, commit_deadline_ms: int) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._commit_write_states[task] = False
        try:
            await self._roundtrip(
                {
                    "method": "spawn_commit",
                    "terminal_id": terminal_id,
                    "spawn_key": spawn_key,
                    "commit_deadline_ms": commit_deadline_ms,
                }
            )
        except asyncio.CancelledError as exc:
            if task is not None:
                exc.__dict__["request_written"] = self._commit_write_states.get(task, False)
            raise
        finally:
            if task is not None:
                self._commit_write_states.pop(task, None)

    async def write(
        self,
        *,
        host_terminal_id: str,
        kind: str,
        data: bytes,
        submit: bool = False,
        operation_seq: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "method": "write",
            "host_terminal_id": host_terminal_id,
            "kind": kind,
            "encoding": "utf8-b64",
            "data": base64.b64encode(data).decode("ascii"),
        }
        if kind == "text":
            payload["submit"] = submit
        return await self._mutating_roundtrip(payload, operation_seq=operation_seq)

    async def write_batch(self, targets: Sequence[HostBatchTarget]) -> list[dict[str, Any]]:
        """Write ordered operations to bounded native targets in one roundtrip."""
        if len(targets) > MAX_WRITE_BATCH_TARGETS:
            raise HostCommandError("too_many_targets")
        payload_bytes = 0
        recipient_ids: set[str] = set()
        terminal_ids: set[str] = set()
        encoded_targets: list[dict[str, Any]] = []
        for target in targets:
            if not target.recipient_id or not target.host_terminal_id:
                raise HostCommandError("invalid_target")
            if target.recipient_id in recipient_ids or target.host_terminal_id in terminal_ids:
                raise HostCommandError("duplicate_target")
            recipient_ids.add(target.recipient_id)
            terminal_ids.add(target.host_terminal_id)
            if (
                not target.operations
                or len(target.operations) > MAX_WRITE_BATCH_OPERATIONS_PER_TARGET
            ):
                raise HostCommandError("too_many_operations")
            total_delay_ms = 0
            operations: list[dict[str, Any]] = []
            for operation in target.operations:
                if operation.kind not in {"text", "key"}:
                    raise HostCommandError("invalid_kind")
                if operation.delay_ms < 0 or operation.delay_ms > MAX_WRITE_BATCH_DELAY_MS:
                    raise HostCommandError("invalid_delay")
                total_delay_ms += operation.delay_ms
                if total_delay_ms > MAX_WRITE_BATCH_TOTAL_DELAY_MS:
                    raise HostCommandError("invalid_delay")
                payload_bytes += len(operation.data)
                if payload_bytes > MAX_WRITE_BATCH_PAYLOAD_BYTES:
                    raise HostCommandError("request_too_large")
                operations.append(
                    {
                        "kind": operation.kind,
                        "encoding": "utf8-b64",
                        "data": base64.b64encode(operation.data).decode("ascii"),
                        "delay_ms": operation.delay_ms,
                    }
                )
            encoded_targets.append(
                {
                    "recipient_id": target.recipient_id,
                    "host_terminal_id": target.host_terminal_id,
                    "operations": operations,
                }
            )
        result = await self._mutating_roundtrip(
            {"method": "write_batch", "targets": encoded_targets}
        )
        raw_results = result.get("results")
        if not isinstance(raw_results, list) or len(raw_results) != len(targets):
            raise HostDecodeError("write_batch response missing per-target results")
        decoded: list[dict[str, Any]] = []
        for target, item in zip(targets, raw_results, strict=True):
            if not isinstance(item, dict):
                raise HostDecodeError("write_batch target result must be an object")
            if item.get("recipient_id") != target.recipient_id:
                raise HostDecodeError("write_batch target results are out of order")
            if item.get("host_terminal_id") != target.host_terminal_id:
                raise HostDecodeError("write_batch target result identifies the wrong terminal")
            if item.get("ok") is True:
                if item.get("written") is not True:
                    raise HostDecodeError("write_batch success result is missing written=true")
            elif item.get("ok") is False:
                if not isinstance(item.get("error"), str) or item.get("stage") not in {
                    "none",
                    "partial",
                }:
                    raise HostDecodeError("write_batch failure result is malformed")
            else:
                raise HostDecodeError("write_batch target result is missing ok")
            decoded.append(item)
        return decoded

    async def kill(self, host_terminal_id: str, grace_ms: int = 50) -> None:
        await self._mutating_roundtrip(
            {
                "method": "kill",
                "host_terminal_id": host_terminal_id,
                "grace_ms": grace_ms,
            }
        )

    async def resize(self, host_terminal_id: str, rows: int, cols: int) -> None:
        await self._mutating_roundtrip(
            {
                "method": "resize",
                "host_terminal_id": host_terminal_id,
                "rows": rows,
                "cols": cols,
            }
        )

    async def snapshot(
        self,
        host_terminal_id: str,
        *,
        mode: SnapshotMode = "text",
        max_bytes: int = 262144,
        max_lines: int = 500,
    ) -> dict[str, Any]:
        payload = await self._roundtrip(
            {
                "method": "snapshot",
                "host_terminal_id": host_terminal_id,
                "mode": mode,
                "max_bytes": max_bytes,
                "max_lines": max_lines,
            }
        )
        echoed = payload.get("mode")
        if echoed != mode:
            # A host that does not echo the mode it honored cannot be trusted to
            # have returned this representation, and plain-text readers break on
            # escape bytes. Refuse instead of handing back the wrong bytes.
            raise HostCommandError(
                "snapshot_mode_mismatch",
                detail=f"requested mode {mode!r}, host answered {echoed!r}",
            )
        return payload

    async def reserve_observer(self, terminal_id: str, reserve_key: str) -> dict[str, Any]:
        return await self._roundtrip(
            {
                "method": "reserve_observer",
                "terminal_id": terminal_id,
                "reserve_key": reserve_key,
            }
        )

    async def release_observer(self, reservation_id: str, reserve_key: str) -> dict[str, Any]:
        return await self._roundtrip(
            {
                "method": "release_observer",
                "reservation_id": reservation_id,
                "reserve_key": reserve_key,
            }
        )

    async def subscribe_events(self, since: int | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"method": "subscribe_events"}
        if since is not None:
            request["since"] = since
        return await self._roundtrip(request)

    @classmethod
    async def open_event_stream(
        cls,
        socket_path: Path,
        control_token: str,
        *,
        since: int | None = None,
    ) -> HostEventStream:
        client = await cls.connect(socket_path)
        try:
            await client.hello(CONTROL_PROTOCOL_VERSION, control_token)
            subscribed = await client.subscribe_events(since)
        except BaseException:
            await client.close()
            raise
        return HostEventStream(
            client,
            epoch=str(subscribed["epoch"]),
            seq=int(subscribed["seq"]),
            gap=bool(subscribed.get("gap", False)),
        )

    async def reconnect(self, socket_path: Path, expected_epoch: str | None = None) -> str:
        # The operation lock is taken first, in the same order _mutating_roundtrip
        # takes it, so no request can hold a sequence allocated against the old
        # connection while this resets the sequence space for the new one.
        async with self._operation_lock, self._lifecycle_lock:
            await self._close_generation("control connection replaced")
            try:
                reader, writer = await asyncio.open_unix_connection(
                    path=str(socket_path), limit=MAX_CONTROL_LINE + 1
                )
            except (OSError, ConnectionError) as exc:
                raise HostUnavailableError("gterm host unavailable") from exc
            self._reader = reader
            self._writer = writer
            self.closed = False
            self.next_seq = 1
            generation = self._generation
            self._reader_task = asyncio.create_task(self._reader_loop(generation))
            if self._control_token is not None:
                hello_future = await self._begin_request(
                    {
                        "method": "hello",
                        "protocol_version": self._protocol_version,
                        "control_token": self._control_token,
                    }
                )
                hello = await hello_future
                self.raise_for_payload(hello)
            ping_future = await self._begin_request({"method": "ping"})
            ping = self.require_ping(await ping_future)
            epoch = str(ping["host_epoch"])
            if expected_epoch is not None and epoch != expected_epoch:
                await self._close_generation("host epoch changed")
                raise HostEpochChangedError("host epoch changed")
            self.host_epoch = epoch
            return epoch


__all__ = [
    "CommitTransportError",
    "CONTROL_PROTOCOL_VERSION",
    "HostClient",
    "HostCommandError",
    "HostDecodeError",
    "HostEpochChangedError",
    "HostManagerStopped",
    "HostNotAdoptedError",
    "HostUnavailableError",
    "decode_control_line",
    "encode_control_line",
]
