"""Async NDJSON client for the gterm control socket."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.terminals.host_protocol import (
    CONTROL_PROTOCOL_VERSION,
    HostListRow,
    decode_line,
    encode_line,
)


class HostControlError(RuntimeError):
    """Typed control-protocol failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


class HostControlClient:
    """Newline-delimited JSON control client."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.closed = False

    @classmethod
    async def connect(cls, socket_path: Path) -> HostControlClient:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        return cls(reader, writer)

    async def hello(
        self,
        protocol_version: int,
        control_token: str,
    ) -> HelloResult:
        payload = await self._roundtrip(
            {
                "method": "hello",
                "protocol_version": protocol_version,
                "control_token": control_token,
            }
        )
        return HelloResult(
            host_epoch=str(payload["host_epoch"]),
            version=str(payload.get("version", "")),
            protocol_version=int(payload.get("protocol_version", CONTROL_PROTOCOL_VERSION)),
        )

    async def ping(self) -> PingResult:
        payload = await self._roundtrip({"method": "ping"})
        return PingResult(
            host_epoch=str(payload["host_epoch"]),
            version=str(payload.get("version", "")),
            host_pid=int(payload["host_pid"]),
        )

    async def list_terminals(self) -> list[HostListRow]:
        payload = await self._roundtrip({"method": "list"})
        raw_rows = payload.get("terminals")
        if not isinstance(raw_rows, list):
            return []
        rows: list[HostListRow] = []
        for item in raw_rows:
            if isinstance(item, dict):
                rows.append(HostListRow.from_mapping(item))
        return rows

    async def host_shutdown(self, grace_ms: int) -> dict[str, bool]:
        payload = await self._roundtrip(
            {"method": "host_shutdown", "grace_ms": max(0, int(grace_ms))}
        )
        return {
            "accepted": bool(payload.get("accepted", True)),
            "draining": bool(payload.get("draining", True)),
        }

    async def spawn_commit(self, terminal_id: str, spawn_key: str) -> None:
        await self._roundtrip(
            {
                "method": "spawn_commit",
                "terminal_id": terminal_id,
                "spawn_key": spawn_key,
            }
        )

    async def kill(self, host_terminal_id: str) -> None:
        await self._roundtrip({"method": "kill", "host_terminal_id": host_terminal_id})

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (OSError, ConnectionError):
            return

    async def _roundtrip(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise ConnectionError("control closed")
        self._writer.write(encode_line(request))
        await self._writer.drain()
        raw = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
        if not raw:
            self.closed = True
            raise ConnectionError("control closed")
        payload = decode_line(raw.decode("utf-8"))
        if payload.get("ok") is False:
            raise HostControlError(str(payload.get("error", "error")))
        return payload
