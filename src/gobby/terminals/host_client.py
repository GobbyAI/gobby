"""Async JSON-lines client for gterm-control.sock (plan 4.1)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gobby.terminals.host_protocol import (
    CONTROL_PROTOCOL_VERSION,
    HostListRow,
    decode_line,
)

MAX_CONTROL_LINE = 2 * 1024 * 1024


class HostEpochChangedError(RuntimeError):
    """Welcome/ping epoch did not match the locator the caller still holds."""


class HostCommandError(RuntimeError):
    """Typed control-protocol refusal (`ok: false`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HostUnavailableError(HostCommandError):
    """The gterm host cannot be reached: a `host_unavailable` refusal, never a tmux fallback."""

    def __init__(self, message: str = "gterm host unavailable") -> None:
        super().__init__("host_unavailable")
        self.message = message


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
            raise HostCommandError(str(payload.get("error", "error")))

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
        if self.closed:
            raise ConnectionError("control closed")
        encoded = encode_control_line(request)
        if len(encoded) >= MAX_CONTROL_LINE:
            raise HostCommandError("request_too_large")
        async with self._lock:
            self._writer.write(encoded)
            await self._writer.drain()
            return await self.read_payload()

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
        await self._roundtrip(
            {"method": "spawn_commit", "terminal_id": terminal_id, "spawn_key": spawn_key}
        )

    async def write(
        self,
        *,
        host_terminal_id: str,
        kind: str,
        data: bytes,
        submit: bool = False,
        operation_seq: int | None = None,
    ) -> dict[str, Any]:
        import base64

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
    "CONTROL_PROTOCOL_VERSION",
    "HostClient",
    "HostCommandError",
    "HostDecodeError",
    "HostEpochChangedError",
    "HostUnavailableError",
    "decode_control_line",
    "encode_control_line",
]
