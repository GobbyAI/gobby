"""Async JSON-lines client for gterm-control.sock (plan 4.1)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from gobby.terminals.host_protocol import (
    CONTROL_PROTOCOL_VERSION,
    HostListRow,
    decode_line,
)

MAX_CONTROL_LINE = 2 * 1024 * 1024
MAX_WRITE_BATCH_TARGETS = 64
MAX_WRITE_BATCH_PAYLOAD_BYTES = 1024 * 1024
MAX_WRITE_BATCH_OPERATIONS_PER_TARGET = 128
MAX_WRITE_BATCH_DELAY_MS = 1_000
MAX_WRITE_BATCH_TOTAL_DELAY_MS = 5_000


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
        self._lock = asyncio.Lock()
        self.closed = False
        self.host_epoch: str | None = None
        self.next_seq = 1
        self._commit_write_states: dict[asyncio.Task[Any], bool] = {}

    @classmethod
    async def connect(cls, socket_path: Path) -> HostClient:
        try:
            reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
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

    @staticmethod
    def require_ping(payload: dict[str, Any]) -> dict[str, Any]:
        host_pid = payload.get("host_pid")
        if not isinstance(host_pid, int):
            raise HostDecodeError("host_pid required")
        return payload

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (OSError, ConnectionError):
            return

    async def read_payload(self) -> dict[str, Any]:
        try:
            raw = await self._reader.readuntil(separator=b"\n")
        except asyncio.LimitOverrunError as exc:
            raise HostCommandError("request_too_large") from exc
        except asyncio.IncompleteReadError as exc:
            self.closed = True
            raise ConnectionError("control closed") from exc
        if not raw:
            self.closed = True
            raise ConnectionError("control closed")
        if len(raw) >= MAX_CONTROL_LINE:
            raise HostCommandError("request_too_large")
        payload = decode_control_line(raw)
        self.raise_for_payload(payload)
        return payload

    async def _roundtrip(self, request: dict[str, Any]) -> dict[str, Any]:
        is_commit = request.get("method") == "spawn_commit"
        request_written = False
        try:
            if self.closed:
                raise ConnectionError("control closed")
            encoded = encode_control_line(request)
            if len(encoded) >= MAX_CONTROL_LINE:
                raise HostCommandError("request_too_large")
            async with self._lock:
                self._writer.write(encoded)
                request_written = True
                task = asyncio.current_task()
                if is_commit and task is not None and task in self._commit_write_states:
                    self._commit_write_states[task] = True
                await self._writer.drain()
                return await self.read_payload()
        except (asyncio.CancelledError, HostCommandError):
            raise
        except (ConnectionError, OSError, TimeoutError) as exc:
            if is_commit:
                raise CommitTransportError(
                    str(exc) or "commit transport failed",
                    request_written=request_written,
                ) from exc
            raise HostUnavailableError(str(exc) or "gterm host unavailable") from exc

    async def hello(self, protocol_version: int, control_token: str) -> HelloResult:
        payload = await self._roundtrip(
            {
                "method": "hello",
                "protocol_version": protocol_version,
                "control_token": control_token,
            }
        )
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
        payload = await self._roundtrip({"method": "list"})
        raw_rows = payload.get("terminals")
        if not isinstance(raw_rows, list):
            return []
        return [HostListRow.from_mapping(item) for item in raw_rows if isinstance(item, dict)]

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
        seq = int(fields.pop("operation_seq", self.next_seq))
        request = {"method": "spawn", "operation_seq": seq, **fields}
        payload = await self._roundtrip(request)
        self.next_seq = seq + 1
        return payload

    async def spawn_commit(self, terminal_id: str, spawn_key: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._commit_write_states[task] = False
        try:
            await self._roundtrip(
                {"method": "spawn_commit", "terminal_id": terminal_id, "spawn_key": spawn_key}
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
        seq = self.next_seq if operation_seq is None else operation_seq
        payload: dict[str, Any] = {
            "method": "write",
            "operation_seq": seq,
            "host_terminal_id": host_terminal_id,
            "kind": kind,
            "encoding": "utf8-b64",
            "data": base64.b64encode(data).decode("ascii"),
        }
        if kind == "text":
            payload["submit"] = submit
        result = await self._roundtrip(payload)
        if operation_seq is None:
            self.next_seq = seq + 1
        return result

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
        seq = self.next_seq
        result = await self._roundtrip(
            {"method": "write_batch", "operation_seq": seq, "targets": encoded_targets}
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
        self.next_seq = seq + 1
        return decoded

    async def kill(self, host_terminal_id: str, grace_ms: int = 50) -> None:
        seq = self.next_seq
        await self._roundtrip(
            {
                "method": "kill",
                "operation_seq": seq,
                "host_terminal_id": host_terminal_id,
                "grace_ms": grace_ms,
            }
        )
        self.next_seq = seq + 1

    async def resize(self, host_terminal_id: str, rows: int, cols: int) -> None:
        seq = self.next_seq
        await self._roundtrip(
            {
                "method": "resize",
                "operation_seq": seq,
                "host_terminal_id": host_terminal_id,
                "rows": rows,
                "cols": cols,
            }
        )
        self.next_seq = seq + 1

    async def snapshot(
        self,
        host_terminal_id: str,
        *,
        mode: str = "text",
        max_bytes: int = 262144,
        max_lines: int = 500,
    ) -> dict[str, Any]:
        return await self._roundtrip(
            {
                "method": "snapshot",
                "host_terminal_id": host_terminal_id,
                "mode": mode,
                "max_bytes": max_bytes,
                "max_lines": max_lines,
            }
        )

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

    async def subscribe_events(self) -> dict[str, Any]:
        return await self._roundtrip({"method": "subscribe_events"})

    async def reconnect(self, socket_path: Path, expected_epoch: str | None = None) -> str:
        await self.close()
        replacement = await HostClient.connect(socket_path)
        self._reader = replacement._reader
        self._writer = replacement._writer
        self.closed = False
        self.next_seq = 1
        ping = await self.ping()
        epoch = str(ping.host_epoch)
        if expected_epoch is not None and epoch != expected_epoch:
            await self.close()
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
    "HostUnavailableError",
    "decode_control_line",
    "encode_control_line",
]
